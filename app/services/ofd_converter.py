import base64
import os
import traceback
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


_patch_easyofd_signature_assert()


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


def _apply_template_backgrounds(pdf_bytes: bytes, fills: dict) -> bytes:
    """
    用 pymupdf 重建 PDF：先把模板背景矩形铺在最底层，再把 easyofd
    产出的原页面内容整体叠加在上层。

    这样同时规避 easyofd 的两个渲染缺陷：只描边不填充、绘制顺序
    忽略 ZOrder=Background（背景最后画会盖住文字）。

    :param pdf_bytes: easyofd to_pdf() 的输出
    :param fills: _extract_fill_rects() 的结果
    :return: 补画背景后的 PDF 字节
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
        return out.tobytes()
    finally:
        src.close()
        out.close()


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
            pdf_bytes = self._ofd.to_pdf()
            self._ofd.del_data()

            # 补画 easyofd 渲染时丢失的模板背景色块（见 _extract_fill_rects 注释）
            if fill_rects:
                total = sum(len(v) for v in fill_rects.values())
                pdf_bytes = _apply_template_backgrounds(pdf_bytes, fill_rects)
                logger.info(f"已补画模板背景填充矩形 {total} 个（{len(fill_rects)} 页）")

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
