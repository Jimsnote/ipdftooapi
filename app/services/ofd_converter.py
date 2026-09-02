import base64
import math
import os
import re
import traceback
import xml.etree.ElementTree as ET
import zipfile
from typing import Tuple

from easyofd.ofd import OFD

from app.core.logger import get_logger

logger = get_logger(__name__)


def _patch_easyofd_signature_assert() -> None:
    """
    修复 easyofd==20260427 对「有签名值但无骑缝章」OFD 的崩溃缺陷。

    缺陷链路（easyofd/parser_ofd/ofd_parser.py）：
        1. SignatureFileParser.__call__ 只有在 XML 里找到 <ofd:StampAnnot>
           时才填充结果 dict，否则返回空 dict {}；
        2. ofd_parser.py 第 ~340 行随后执行
               SignedValue = signatures_info.get("SignedValue")   # -> None
               self.get_xml_obj(SignedValue)                       # -> assert label 抛错
        3. get_xml_obj 开头是裸断言 `assert label`，对 None 抛无消息
           AssertionError()，调用方 str(e) 得到空串，前端只显示
           “OFD 转换失败: ”（一片空白），无法排障。

    触发条件：OFD 含 <ofd:Signatures>，且 Signature.xml 只有
    <ofd:SignedInfo>/<ofd:SignedValue> 而没有 <ofd:StampAnnot>。
    铁路 12306 电子发票（Provider=ChinaRailway12306）等即属此类。

    修复：给 get_xml_obj 加空值保护——label 为空时返回 ""，这与该函数
    「找不到就返回空串」的既有契约一致，签名信息缺失不影响版式转换结果。
    """
    try:
        from easyofd.parser_ofd.ofd_parser import OFDParser
    except Exception as e:  # pragma: no cover - 依赖缺失时不应阻断应用启动
        logger.warning(f"easyofd OFDParser 导入失败，跳过签名兼容补丁: {e}")
        return

    if getattr(OFDParser.get_xml_obj, "_ipdftoo_patched", False):
        return

    original_get_xml_obj = OFDParser.get_xml_obj

    def get_xml_obj_safe(self, label):
        if not label:
            return ""
        return original_get_xml_obj(self, label)

    get_xml_obj_safe._ipdftoo_patched = True
    OFDParser.get_xml_obj = get_xml_obj_safe
    logger.info("已应用 easyofd 签名兼容补丁（无 StampAnnot 的 SignedValue 空值保护）")


def _patch_easyofd_cmp_offset() -> None:
    """
    修复 easyofd==20260427 cmp_offset 对 CTM 缩放的应用缺陷。

    缺陷（draw_pdf.py cmp_offset，"g N v" 重复计数字距语法分支）：
        if offset_i == "g":
            for j in range(int(offsets[g_no + 1])):
                char_pos += float(offsets[g_no + 2])   # ← 缺 * resize
    g 分支的字距累加没有乘 CTM 的水平/垂直缩放系数（resize），
    而普通数值分支有 `* resize`。带 CTM="0.89 0 0 1" 的文本（机票式
    电子发票的「统一社会信用代码/纳税人识别号：」标签）字距不收缩，
    整行比 Boundary 宽 ~12%，把右侧 18 位信用代码的头两位盖住。

    修复：g 分支同样乘 resize（与普通分支对齐）。
    """
    try:
        from easyofd.draw.draw_pdf import DrawPDF
    except Exception as e:  # pragma: no cover
        logger.warning(f"easyofd DrawPDF 导入失败，跳过字距缩放补丁: {e}")
        return

    if getattr(DrawPDF.cmp_offset, "_ipdftoo_patched", False):
        return

    def cmp_offset_fixed(self, pos, offset, DeltaRule, text, CTM_info, dire="X") -> list:
        if CTM_info and dire == "X":
            resize = CTM_info.get("resizeX")
            move = CTM_info.get("moveX")
        elif CTM_info and dire == "Y":
            resize = CTM_info.get("resizeY")
            move = CTM_info.get("moveY")
        else:
            resize = 1
            move = 0

        char_pos = float(pos if pos else 0) + (float(offset if offset else 0) + move) * resize
        pos_list = [char_pos]
        offsets = DeltaRule.split(" ")

        if "g" in DeltaRule:  # g <count> <value>：count 个重复字距
            g_no = None
            for _no, offset_i in enumerate(offsets):
                if offset_i == "g":
                    g_no = _no
                    for _ in range(int(offsets[g_no + 1])):
                        char_pos += float(offsets[g_no + 2]) * resize
                        pos_list.append(char_pos)
                elif offset_i and offset_i != "g":
                    if g_no is None:
                        char_pos += float(offset_i) * resize
                        pos_list.append(char_pos)
                    elif int(_no) > int(g_no + 2):
                        char_pos += float(offset_i) * resize
                        pos_list.append(char_pos)
        elif not DeltaRule:  # 无字距：所有字符同一位置（单字符场景）
            pos_list = [char_pos for _ in text]
        else:
            for i in offsets:
                char_pos += (float(i) if i else 0) * resize
                pos_list.append(char_pos)
        return pos_list

    cmp_offset_fixed._ipdftoo_patched = True
    DrawPDF.cmp_offset = cmp_offset_fixed
    logger.info("已应用 easyofd 字距缩放补丁（g 分支乘 CTM resize）")


_patch_easyofd_signature_assert()
_patch_easyofd_cmp_offset()


def _is_plain_rect(abbr: str) -> bool:
    """判断 AbbreviatedData 是否为「M x y L x y L x y L x y C」的简单闭合矩形。"""
    tokens = (abbr or "").split()
    if len(tokens) != 13 or tokens[0] != "M" or tokens[12] != "C":
        return False
    if tokens[3] != "L" or tokens[6] != "L" or tokens[9] != "L":
        return False
    try:
        [float(t) for t in (tokens[1], tokens[2], tokens[4], tokens[5],
                             tokens[7], tokens[8], tokens[10], tokens[11])]
    except ValueError:
        return False
    return True


def _extract_fill_rects(ofd_data) -> dict:
    """
    从 easyofd 解析结果中提取「整块填充矩形」（模板背景）。

    easyofd 的 draw_pdf.draw_line 只描边不填充（从不调 setFillColorRGB，
    且 drawPath 默认 fill=0），导致 OFD 模板层的背景色块在转 PDF 时全部
    丢失（如铁路电子客票的浅蓝底与底部条带）。本函数把解析数据里
    line_list 中带 FillColor 的简单闭合矩形提取出来，供后处理补画。

    :param ofd_data: OFD().read() 之后的 self.data（list[dict]）
    :return: {page_no: [(rgb(0-255), (nx0, ny0, nx1, ny1) 归一化坐标 y 向下)]}
    """
    fills = {}
    for doc in ofd_data or []:
        page_sizes = doc.get("page_size") or []

        def _page_dim(idx):
            if idx < len(page_sizes) and len(page_sizes[idx]) >= 4:
                return page_sizes[idx][2], page_sizes[idx][3]
            if page_sizes and len(page_sizes[0]) >= 4:
                return page_sizes[0][2], page_sizes[0][3]
            return None

        for page_no, content in (doc.get("page_info") or {}).items():
            try:
                key = int(page_no)
            except (TypeError, ValueError):
                continue
            dim = _page_dim(key)
            if not dim or dim[0] <= 0 or dim[1] <= 0:
                continue
            pw, ph = float(dim[0]), float(dim[1])

            for line in content.get("line_list") or []:
                color = line.get("FillColor")
                if not isinstance(color, (list, tuple)) or len(color) < 3:
                    continue
                try:
                    rgb = tuple(int(c) for c in color[:3])
                except (TypeError, ValueError):
                    continue
                if not all(0 <= v <= 255 for v in rgb):
                    continue
                if not _is_plain_rect(line.get("AbbreviatedData")):
                    continue
                pos = line.get("pos") or []
                if len(pos) < 4:
                    continue
                try:
                    x, y, w, h = (float(v) for v in pos[:4])
                except (TypeError, ValueError):
                    continue
                if w <= 0 or h <= 0:
                    continue
                # 归一化到 [0,1]（OFD 坐标 y 向下，与 pymupdf 一致）
                rect = (
                    max(0.0, min(1.0, x / pw)),
                    max(0.0, min(1.0, y / ph)),
                    max(0.0, min(1.0, (x + w) / pw)),
                    max(0.0, min(1.0, (y + h) / ph)),
                )
                fills.setdefault(key, []).append((rgb, rect))
    return fills


def _parse_ofd_color(elem) -> tuple:
    """
    解析 <ofd:StrokeColor>/<ofd:FillColor> 元素为 (r, g, b)。

    实测各开票方混用 ColorSpace=2 与 4 表达 RGB 三元组（如 255 0 0），
    故按分量数判别而非 ColorSpace 属性：3 个分量按 RGB，1 个分量按
    灰度展开，其余（CMYK/专色等）返回 None 保守跳过。
    """
    try:
        nums = [float(v) for v in (elem.get("Value") or "").split()]
    except ValueError:
        return None
    if len(nums) == 3:
        rgb = nums
    elif len(nums) == 1:
        rgb = [nums[0]] * 3
    else:
        return None
    return tuple(max(0, min(255, int(v))) for v in rgb)


def _load_drawparam_colors(input_path: str) -> dict:
    """
    直接从 OFD zip 的资源 XML 中提取 DrawParam 颜色表。

    easyofd 只解析 PublicRes 中的 DrawParams（ofd_parser.py:283），
    定义在 DocumentRes 里的 DrawParams 被整体丢弃，导致
    doc["drawparams"] 为空 dict——凡「颜色只存在于 DocumentRes
    DrawParam」的线条（如机票式电子发票的红色表格线）转 PDF 时
    全部落入 draw_line 的默认黑色分支。

    :param input_path: OFD 文件路径
    :return: {drawparam_id: {"StrokeColor": (r,g,b), "FillColor": (r,g,b)}}
    """
    params = {}
    try:
        with zipfile.ZipFile(input_path) as zf:
            res_files = [
                n for n in zf.namelist()
                if re.search(r"Doc_\d+/(?:Document|Public)Res[\d_]*\.xml$", n)
            ]
            for name in res_files:
                try:
                    root = ET.fromstring(zf.read(name))
                except ET.ParseError:
                    continue
                for dp in root.iter(f"{{http://www.ofdspec.org/2016}}DrawParam"):
                    pid = dp.get("ID")
                    if not pid:
                        continue
                    info = {}
                    for child in dp:
                        tag = child.tag.rsplit("}", 1)[-1]
                        if tag in ("StrokeColor", "FillColor"):
                            rgb = _parse_ofd_color(child)
                            if rgb:
                                info[tag] = rgb
                    if info:
                        params[pid] = info
    except Exception as e:
        logger.warning(f"解析 OFD DrawParam 失败（跳过颜色修补）: {e!r}")
    return params


def _valid_rgb(color) -> tuple:
    """校验 easyofd 解析出的颜色值为 3 个数值，返回 tuple 或 None。"""
    if not isinstance(color, (list, tuple)) or len(color) < 3:
        return None
    try:
        vals = [float(v) for v in color[:3]]
    except (TypeError, ValueError):
        return None
    if not any(v for v in vals):
        return None
    return tuple(max(0, min(255, int(v))) for v in vals)


def _apply_drawparam_stroke_colors(ofd_data, drawparams: dict) -> int:
    """
    给「无行内颜色」的线条与文字补上 DrawParam 颜色（修复①，通用版）。

    easyofd 渲染缺陷：
      - draw_line 的赋色逻辑只查 FillColor（行内的和 DrawParam 的），
        取出的 d_p_stroke_color 是从未使用的死代码；
      - draw_chars 的赋色只查行内 color 和 drawparams 的 FillColor，
        而 easyofd 只解析 PublicRes 的 DrawParams——DocumentRes 里的
        整体丢弃（doc["drawparams"] 为空 dict）。
    后果：机票式电子发票的红色标题/表格线/标签文字（颜色全部定义在
    DocumentRes DrawParam 13: FillColor 128 0 0）转 PDF 后一律黑色。

    本函数在 to_pdf 前把颜色直接填进 line["FillColor"] 与
    text["color"]，让 easyofd 现有逻辑恢复正确颜色。

    线条取色优先级（OFD 规范：行内定义覆盖 DrawParam）：
        行内 StrokeColor → DrawParam StrokeColor → DrawParam FillColor
    文字取色优先级（文字以填充为主）：
        行内 FillColor → DrawParam FillColor → DrawParam StrokeColor

    注意：必须安排在 _extract_fill_rects 之后调用，避免把 DrawParam
    填色矩形误判为模板背景。

    :param ofd_data: OFD().read() 之后的 self.data
    :param drawparams: _load_drawparam_colors() 的结果
    :return: 修补的线条/文字数量
    """
    patched = 0
    for doc in ofd_data or []:
        for content in (doc.get("page_info") or {}).values():
            for line in content.get("line_list") or []:
                if _valid_rgb(line.get("FillColor")):
                    continue  # 已有有效行内填充色
                color = _valid_rgb(line.get("StrokeColor"))
                if color is None:
                    pid = line.get("DrawParam")
                    info = drawparams.get(str(pid)) if pid else None
                    if info:
                        color = info.get("StrokeColor") or info.get("FillColor")
                if color:
                    line["FillColor"] = list(color)
                    patched += 1
            for text in content.get("text_list") or []:
                if _valid_rgb(text.get("color")):
                    continue  # 已有有效行内颜色
                color = None
                pid = text.get("DrawParam")
                info = drawparams.get(str(pid)) if pid else None
                if info:
                    color = info.get("FillColor") or info.get("StrokeColor")
                if color:
                    text["color"] = list(color)
                    patched += 1
    return patched


def _parse_abbreviated_path(abbr: str):
    """
    解析 OFD AbbreviatedData 路径指令为线段/贝塞尔序列。

    :param abbr: 如 "M 1 10.5 B x1 y1 x2 y2 x3 y3 ... C"
    :return: [seg, ...]，seg 为
             ("M", (x, y)) 子路径起点；
             ("L", (x, y)) 直线终点；
             ("B", (p1, p2, p3)) 三次贝塞尔（起点为当前点）；
             ("C", None) 闭合（当前点连回子路径起点）
    """
    tokens = (abbr or "").split()
    segs = []
    i = 0
    while i < len(tokens):
        op = tokens[i]
        i += 1
        if op == "M" or op == "S":
            try:
                segs.append(("M", (float(tokens[i]), float(tokens[i + 1]))))
            except (ValueError, IndexError):
                return []
            i += 2
        elif op == "L":
            try:
                segs.append(("L", (float(tokens[i]), float(tokens[i + 1]))))
            except (ValueError, IndexError):
                return []
            i += 2
        elif op == "B":
            try:
                pts = tuple(float(v) for v in tokens[i:i + 6])
            except ValueError:
                return []
            if len(pts) < 6:
                return []
            segs.append(("B", (pts[0:2], pts[2:4], pts[4:6])))
            i += 6
        elif op == "Q":
            try:
                pts = tuple(float(v) for v in tokens[i:i + 4])
            except ValueError:
                return []
            if len(pts) < 4:
                return []
            segs.append(("B", (pts[0:2], pts[2:4], pts[2:4])))  # 二次贝塞尔退化为三次
            i += 4
        elif op == "A":
            # 圆弧：发票矢量章中不出现，跳过（保守：放弃整个路径）
            return []
        elif op == "C":
            segs.append(("C", None))
        else:
            return []
    return segs


def _extract_vector_stamps(input_path: str, ofd_data) -> dict:
    """
    提取 easyofd 完全不支持的 CompositeObject 矢量图形（修复②）。

    easyofd 解析器/绘制器均未实现 CompositeObject（全包 grep 零匹配），
    页面引用的 CompositeGraphicUnit（CGU）会被静默丢弃——铁路数电票的
    「全国统一发票监制章」正是 CGU 实现的椭圆矢量章（2 圈贝塞尔椭圆
    + 沿弧旋转排布文字 + 中心横排文字），表现为转 PDF 后税局章消失。

    坐标语义（OFD 规范，均为 mm、y 向下）：
      CGU 内对象点 = CompositeObject.Boundary.xy + 对象自身偏移；
      PathObject: 点 = P.Boundary.xy + AbbreviatedData 局部坐标；
      TextObject: 字符基线 = T.Boundary.xy + CTM × (X + ΣΔX, Y)，
                  字形方向角 = atan2(CTM.b, CTM.a)。

    :param input_path: OFD 文件路径
    :param ofd_data: OFD().read() 之后的 self.data（用于取页面尺寸）
    :return: {page_no: [stamp, ...]}，stamp 为
             {"paths": [{"segs": [...], "color": (r,g,b), "width": mm}],
              "texts": [{"char": str, "x": mm, "y": mm, "angle": deg,
                         "size": mm, "color": (r,g,b)}]}
             所有坐标已换算为页面 mm 坐标（原点左上、y 向下）。
    """
    OFD_NS = "http://www.ofdspec.org/2016"
    stamps = {}
    try:
        with zipfile.ZipFile(input_path) as zf:
            names = zf.namelist()
            # 页面序号 -> Content.xml 路径（按 Page_N 目录序）
            page_files = sorted(
                (n for n in names if re.search(r"Doc_\d+/Pages/Page_\d+/Content\.xml$", n)),
                key=lambda n: int(re.search(r"Page_(\d+)", n).group(1)),
            )
            res_files = [n for n in names
                         if re.search(r"Doc_\d+/(?:Document|Public)Res[\d_]*\.xml$", n)]

            # 解析所有 CGU：{ResourceID: CGU element}
            cgus = {}
            for name in res_files:
                try:
                    root = ET.fromstring(zf.read(name))
                except ET.ParseError:
                    continue
                for cgu in root.iter(f"{{{OFD_NS}}}CompositeGraphicUnit"):
                    rid = cgu.get("ID")
                    if rid:
                        cgus[rid] = cgu
            if not cgus:
                return stamps

            for pg_no, page_file in enumerate(page_files):
                try:
                    proot = ET.fromstring(zf.read(page_file))
                except ET.ParseError:
                    continue
                page_stamps = []
                # 页面里每个 CompositeObject 引用一个 CGU
                for obj in proot.iter(f"{{{OFD_NS}}}CompositeObject"):
                    rid = obj.get("ResourceID")
                    cgu = cgus.get(rid)
                    if cgu is None:
                        continue
                    try:
                        bx, by = float(obj.get("Boundary").split()[0]), \
                                 float(obj.get("Boundary").split()[1])
                    except (ValueError, AttributeError):
                        continue

                    stamp = {"paths": [], "texts": []}
                    # CGU 内容对象
                    for po in cgu.iter(f"{{{OFD_NS}}}PathObject"):
                        color = None
                        for c in po:
                            if c.tag.rsplit("}", 1)[-1] == "StrokeColor":
                                color = _parse_ofd_color(c)
                        try:
                            width = float(po.get("LineWidth") or "0.25")
                        except ValueError:
                            width = 0.25
                        pb = po.get("Boundary", "0 0 0 0").split()
                        try:
                            ox, oy = float(pb[0]), float(pb[1])
                        except (ValueError, IndexError):
                            ox, oy = 0.0, 0.0
                        segs = _parse_abbreviated_path(po.findtext(
                            f"{{{OFD_NS}}}AbbreviatedData", ""))
                        if not segs:
                            continue
                        # 局部坐标 -> 页面 mm 坐标
                        abs_segs = []
                        for op, pts in segs:
                            if op == "B":
                                abs_segs.append(("B", tuple(
                                    (bx + ox + p[0], by + oy + p[1]) for p in pts)))
                            elif pts is not None:
                                abs_segs.append((op, (bx + ox + pts[0], by + oy + pts[1])))
                            else:
                                abs_segs.append((op, None))
                        stamp["paths"].append({
                            "segs": abs_segs, "color": color or (0, 0, 0),
                            "width": width,
                        })

                    for to in cgu.iter(f"{{{OFD_NS}}}TextObject"):
                        color = None
                        for c in to:
                            if c.tag.rsplit("}", 1)[-1] == "FillColor":
                                color = _parse_ofd_color(c)
                        try:
                            size = float(to.get("Size") or "3")
                        except ValueError:
                            size = 3.0
                        tb = to.get("Boundary", "0 0 0 0").split()
                        try:
                            tx, ty = float(tb[0]), float(tb[1])
                        except (ValueError, IndexError):
                            tx, ty = 0.0, 0.0
                        # CTM（缺省单位阵）：[a b c d e f]
                        a, b_, c_, d, e, f = 1.0, 0.0, 0.0, 1.0, 0.0, 0.0
                        ctm = (to.get("CTM") or "").split()
                        if len(ctm) == 6:
                            try:
                                a, b_, c_, d, e, f = (float(v) for v in ctm)
                            except ValueError:
                                a, b_, c_, d, e, f = 1.0, 0.0, 0.0, 1.0, 0.0, 0.0
                        angle = math.degrees(math.atan2(b_, a))
                        for tc in to.iter(f"{{{OFD_NS}}}TextCode"):
                            text = (tc.text or "").strip()
                            if not text:
                                continue
                            try:
                                x0 = float(tc.get("X") or "0")
                                y0 = float(tc.get("Y") or "0")
                            except ValueError:
                                continue
                            # DeltaX：逐字水平间距
                            dx_list = [float(v) for v in (tc.get("DeltaX") or "").split()
                                       if re.match(r"^-?[\d.]+$", v)]
                            for idx, ch in enumerate(text):
                                dx = x0 + sum(dx_list[:idx])
                                # Boundary 原点 + CTM × (x, y)（含平移分量）
                                ux = a * dx + c_ * y0 + e
                                uy = b_ * dx + d * y0 + f
                                stamp["texts"].append({
                                    "char": ch,
                                    "x": bx + tx + ux,
                                    "y": by + ty + uy,
                                    "angle": angle,
                                    "size": size,
                                    "color": color or (0, 0, 0),
                                })
                    if stamp["paths"] or stamp["texts"]:
                        page_stamps.append(stamp)
                if page_stamps:
                    stamps[pg_no] = page_stamps
    except Exception as e:
        logger.warning(f"解析 OFD 矢量章失败（跳过重画）: {e!r}")
    return stamps


def _apply_template_backgrounds(pdf_bytes: bytes, fills: dict,
                                stamps: dict = None) -> bytes:
    """
    用 pymupdf 重建 PDF，按正确层级重排内容：
        底层：模板背景填充矩形（fills）
        中层：easyofd 产出的原页面内容
        顶层：矢量章（stamps，easyofd 不支持 CompositeObject 的补救）

    规避 easyofd 的三个渲染缺陷：只描边不填充、绘制顺序忽略
    ZOrder=Background、CompositeObject 整体丢失（矢量章消失）。

    :param pdf_bytes: easyofd to_pdf() 的输出
    :param fills: _extract_fill_rects() 的结果
    :param stamps: _extract_vector_stamps() 的结果
    :return: 重建后的 PDF 字节
    """
    import fitz

    src = fitz.open(stream=pdf_bytes, filetype="pdf")
    out = fitz.open()
    try:
        for pno in range(src.page_count):
            spage = src[pno]
            rect = spage.rect
            npage = out.new_page(width=rect.width, height=rect.height)
            for rgb, (nx0, ny0, nx1, ny1) in fills.get(pno, []):
                npage.draw_rect(
                    fitz.Rect(nx0 * rect.width, ny0 * rect.height,
                              nx1 * rect.width, ny1 * rect.height),
                    color=None,
                    fill=tuple(v / 255 for v in rgb),
                    width=0,
                )
            npage.show_pdf_page(rect, src, pno)
            # 顶层：重画矢量章（CompositeObject 补救）
            for stamp in (stamps or {}).get(pno, []):
                _draw_vector_stamp(npage, stamp)
        return out.tobytes()
    finally:
        src.close()
        out.close()


def _draw_vector_stamp(page, stamp: dict) -> None:
    """
    用 pymupdf 在页面上重画一个矢量章（CompositeGraphicUnit）。

    :param page: fitz 页面对象
    :param stamp: _extract_vector_stamps() 输出的单个章
    """
    import fitz

    # mm -> pt 换算：easyofd 的 OP 系数（200/25.4），保证与
    # easyofd 输出页面的缩放一致，否则章会画偏/画小
    scale = 200.0 / 25.4

    for path in stamp.get("paths", []):
        shape = page.new_shape()
        cur = None
        start = None
        moved = False
        for op, pts in path["segs"]:
            if op == "M":
                start = fitz.Point(pts[0] * scale, pts[1] * scale)
                cur = start
                moved = False
            elif op == "L" and cur is not None:
                end = fitz.Point(pts[0] * scale, pts[1] * scale)
                shape.draw_line(cur, end)
                cur = end
                moved = True
            elif op == "B" and cur is not None:
                p1, p2, p3 = (fitz.Point(p[0] * scale, p[1] * scale) for p in pts)
                shape.draw_bezier(cur, p1, p2, p3)
                cur = p3
                moved = True
            elif op == "C" and cur is not None and start is not None:
                if moved:
                    shape.draw_line(cur, start)
                cur = start
        if moved:
            shape.finish(
                color=tuple(v / 255 for v in path["color"]),
                width=max(0.1, path["width"] * scale),
                fill=None,
            )
            shape.commit()

    for t in stamp.get("texts", []):
        pt = fitz.Point(t["x"] * scale, t["y"] * scale)
        fontsize = t["size"] * scale
        rot = fitz.Matrix(1, 1).prerotate(t["angle"]) if t["angle"] else None
        try:
            page.insert_text(
                pt,
                t["char"],
                fontsize=fontsize,
                fontname="china-s",  # pymupdf 内置 CJK 字体，服务器零依赖
                color=tuple(v / 255 for v in t["color"]),
                morph=(pt, rot) if rot else None,
            )
        except Exception as e:
            logger.warning(f"矢量章文字绘制失败（{t['char']}）: {e!r}")


class OFDConverter:
    """OFD 版式文档转换服务（基于 easyofd）"""

    def __init__(self):
        self._ofd = OFD()

    def ofd_to_pdf(self, input_path: str, output_path: str) -> Tuple[bool, str]:
        """
        将 OFD 文件转换为 PDF。

        :param input_path: 输入 OFD 文件路径
        :param output_path: 输出 PDF 文件路径
        :return: (是否成功, 结果消息)
        """
        try:
            with open(input_path, "rb") as f:
                ofd_b64 = str(base64.b64encode(f.read()), "utf-8")

            self._ofd.read(ofd_b64, save_xml=False)
            # 在 to_pdf 前提取模板背景（to_pdf/del_data 后数据即释放）
            fill_rects = _extract_fill_rects(self._ofd.data)
            # 修复①：给只定义 DrawParam StrokeColor 的线条补色（须在
            # _extract_fill_rects 之后，避免 DrawParam 填色矩形误判为背景）
            drawparams = _load_drawparam_colors(input_path)
            patched_lines = _apply_drawparam_stroke_colors(self._ofd.data, drawparams)
            if patched_lines:
                logger.info(f"已为 {patched_lines} 条线条补上 DrawParam 描边色")
            # 修复②：提取 easyofd 不支持的 CompositeObject 矢量章
            vector_stamps = _extract_vector_stamps(input_path, self._ofd.data)
            if vector_stamps:
                total = sum(len(v) for v in vector_stamps.values())
                logger.info(f"检测到矢量章 {total} 个（{len(vector_stamps)} 页），将重画")
            pdf_bytes = self._ofd.to_pdf()
            self._ofd.del_data()

            # 补画 easyofd 渲染时丢失的模板背景色块与矢量章
            if fill_rects or vector_stamps:
                total = sum(len(v) for v in fill_rects.values())
                pdf_bytes = _apply_template_backgrounds(pdf_bytes, fill_rects,
                                                        vector_stamps)
                logger.info(f"已补画模板背景填充矩形 {total} 个"
                            f"（{len(fill_rects)} 页）")

            with open(output_path, "wb") as f:
                f.write(pdf_bytes)

            logger.info(f"OFD 转 PDF 成功: {input_path} -> {output_path}")
            return True, output_path

        except Exception as e:
            # 记录完整堆栈：easyofd 会抛无消息异常（如裸 assert），
            # 只记 str(e) 会得到空串，线上将无法排障。
            logger.error(f"OFD 转 PDF 失败: {e!r}\n{traceback.format_exc()}")
            # 确保内存释放
            try:
                self._ofd.del_data()
            except Exception:
                pass
            # str(e) 可能为空串，回退到异常类名，保证调用方能拿到可读信息
            return False, str(e) or type(e).__name__

    def ofd_to_images(self, input_path: str, output_dir: str) -> Tuple[bool, list]:
        """
        将 OFD 文件逐页转为图片（用于预览）。

        :param input_path: 输入 OFD 文件路径
        :param output_dir: 图片输出目录
        :return: (是否成功, 图片路径列表)
        """
        try:
            with open(input_path, "rb") as f:
                ofd_b64 = str(base64.b64encode(f.read()), "utf-8")

            self._ofd.read(ofd_b64, save_xml=False)
            img_list = self._ofd.to_jpg()
            self._ofd.del_data()

            saved_paths = []
            for idx, img in enumerate(img_list):
                from PIL import Image

                im = Image.fromarray(img)
                path = os.path.join(output_dir, f"page_{idx + 1}.jpg")
                im.save(path, quality=95)
                saved_paths.append(path)

            logger.info(f"OFD 转图片成功: {len(saved_paths)} 页")
            return True, saved_paths

        except Exception as e:
            logger.error(f"OFD 转图片失败: {e!r}\n{traceback.format_exc()}")
            try:
                self._ofd.del_data()
            except Exception:
                pass
            return False, []
