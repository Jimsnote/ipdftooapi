"""PDF 涂黑/脱敏（真删除）服务。

设计依据：docs/2026-09-14_231000-redact-pdf-true-redaction-design.md（§3.2–3.6）
- 手术引擎：PyMuPDF apply_redactions()（AGPL 路线，2026-09-15 拍板，禁止回退 pikepdf 手写手术）
- 验证通道：pdfminer.six + pypdfium2 双独立引擎（与手术引擎隔离——同引擎自证无效）
- 扫描件/图片页：pypdfium2 渲染 + PIL 像素涂黑（原图文字本不存在，天然真删除）
- 铁律：验证不通过绝不返回文件；报告不回显被删原文（防二次泄露）
"""
import base64
import io
import json
import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import fitz  # PyMuPDF
from PIL import Image, ImageDraw
from app.core.exceptions import PDFProcessingError
from app.core.logger import get_logger

logger = get_logger(__name__)

# 上限与设计 §3.7 对齐（nginx proxy_read_timeout 60s 约束，M1 实测后放宽）
RASTER_DPI = 150
HIT_EXPAND_PT = 1.0  # 命中矩形四边外扩——宁宽勿窄（宁可多删，不可漏删）

# 中国特供正则预设（设计 §2.1）
ID_PATTERN = (
    r"(?<!\d)[1-9]\d{5}(?:18|19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)"
)
PHONE_PATTERN = r"(?<!\d)1[3-9]\d{9}(?!\d)"
EMAIL_PATTERN = r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"
PRESET_PATTERNS: Dict[str, str] = {
    "id": ID_PATTERN,
    "phone": PHONE_PATTERN,
    "email": EMAIL_PATTERN,
}


@dataclass
class Hit:
    """一个涂黑命中：page 为 0 基索引，rect 为 PyMuPDF 视觉坐标（左上原点 y 向下）。"""

    page: int
    rect: fitz.Rect
    source: str  # "rect" | "keyword" | "preset"
    preset: Optional[str] = None  # 命中的预设 id（preset 命中时非空）
    literal: Optional[str] = None  # 命中的字面量（验证目标，不进报告）


# ---------- 参数解析（纯函数，供路由与测试复用） ----------


def parse_rects(raw: object) -> List[dict]:
    """解析并校验 rects 参数：[{page(1基), x, y, w, h}]，返回清洗后的列表。

    坐标约定：视觉坐标——左上原点、y 向下、cropbox 相对、单位 pt
    （前端 = css 像素 / pdf.js viewport.scale），与 PyMuPDF 全 API 同系。
    """
    if not isinstance(raw, list):
        raise PDFProcessingError("rects 参数必须是数组")
    cleaned: List[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            raise PDFProcessingError("rects 元素必须是对象")
        try:
            page = int(item["page"])
            x = float(item["x"])
            y = float(item["y"])
            w = float(item["w"])
            h = float(item["h"])
        except (KeyError, TypeError, ValueError):
            raise PDFProcessingError("rects 元素缺少 page/x/y/w/h 或数值非法")
        if page < 1:
            raise PDFProcessingError("rects 的 page 从 1 开始")
        if w <= 0 or h <= 0:
            raise PDFProcessingError("rects 的宽高必须大于 0")
        if x < 0 or y < 0:
            raise PDFProcessingError("rects 坐标不能为负")
        cleaned.append({"page": page, "x": x, "y": y, "w": w, "h": h})
    return cleaned


def parse_keywords(raw: object) -> Dict[str, list]:
    """解析并校验 keywords 参数：{presets: ["id",...], custom: ["张三",...]}。"""
    if not isinstance(raw, dict):
        raise PDFProcessingError("keywords 参数必须是对象")
    presets = raw.get("presets", [])
    custom = raw.get("custom", [])
    if not isinstance(presets, list) or not isinstance(custom, list):
        raise PDFProcessingError("keywords.presets / keywords.custom 必须是数组")
    unknown = [p for p in presets if p not in PRESET_PATTERNS]
    if unknown:
        raise PDFProcessingError(f"未知的预设关键词：{','.join(map(str, unknown))}")
    cleaned_custom = []
    for kw in custom:
        if not isinstance(kw, str):
            raise PDFProcessingError("自定义关键词必须是字符串")
        kw = kw.strip()
        if not kw:
            continue
        if len(kw) > 100:
            raise PDFProcessingError("单个自定义关键词不能超过 100 字符")
        cleaned_custom.append(kw)
    return {"presets": [str(p) for p in presets], "custom": cleaned_custom}


# ---------- 内部工具 ----------


def _open_doc(data: bytes) -> fitz.Document:
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        raise PDFProcessingError("无法解析 PDF 文件，文件可能已损坏")
    if doc.needs_pass or doc.is_encrypted:
        doc.close()
        raise PDFProcessingError("该 PDF 已加密，请先使用「解除 PDF 密码」工具解密后再涂黑")
    return doc


def _check_pages(doc: fitz.Document, max_pages: int) -> None:
    if len(doc) > max_pages:
        raise PDFProcessingError(
            f"PDF 页数超过上限（最多 {max_pages} 页，当前 {len(doc)} 页），请拆分后分次处理"
        )


def _expand(rect: fitz.Rect) -> fitz.Rect:
    """命中矩形四边外扩——宁宽勿窄。"""
    return fitz.Rect(
        rect.x0 - HIT_EXPAND_PT,
        rect.y0 - HIT_EXPAND_PT,
        rect.x1 + HIT_EXPAND_PT,
        rect.y1 + HIT_EXPAND_PT,
    )


def _collect_hits(
    doc: fitz.Document, mode: str, rects: List[dict], keywords: Dict[str, list]
) -> Tuple[List[Hit], Set[int]]:
    """收集全部命中矩形。

    返回 (hits, image_pages)：
    - hits: 涂黑命中列表（rects 模式=用户框选；keywords 模式=引擎定位）
    - image_pages: 扫描件/图片页集合（0 基，页文本 <5 字符）
    """
    hits: List[Hit] = []
    image_pages: Set[int] = set()
    presets: List[str] = keywords.get("presets", []) if keywords else []
    custom: List[str] = keywords.get("custom", []) if keywords else []

    for page_index in range(len(doc)):
        page = doc[page_index]
        if len(page.get_text().strip()) < 5:
            image_pages.add(page_index)

        if mode == "rects":
            # 前端(pdf.js)发送视觉坐标：左上原点、y 向下、cropbox 相对、单位 pt
            # （= css 像素 / viewport.scale）。这与 PyMuPDF 全部 API（get_text/
            # search_for/add_redact_annot）同一坐标系，直接使用即可。
            # 注意：不能用 convertToPdfPoint 的输出（PDF 原生 y-up，左下原点），
            # 后端按 y-down 解释时会整页垂直镜像（2026-09-15 report.pdf 实测事故）。
            for r in rects:
                if int(r["page"]) != page_index + 1:
                    continue
                rect = fitz.Rect(r["x"], r["y"], r["x"] + r["w"], r["y"] + r["h"])
                if rect.is_empty or not rect.intersects(page.rect):
                    continue
                hits.append(Hit(page=page_index, rect=_expand(rect), source="rect"))
            continue

        # keywords 模式
        for kw in custom:
            for found in page.search_for(kw):
                hits.append(
                    Hit(page=page_index, rect=_expand(found), source="keyword", literal=kw)
                )
        if presets:
            # CJK 无空格整行一词：逐 span 正则匹配出字面量，再 search_for 拿精确矩形
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        span_text = span.get("text", "")
                        if not span_text:
                            continue
                        for pid in presets:
                            for m in re.finditer(PRESET_PATTERNS[pid], span_text):
                                literal = m.group(0)
                                for found in page.search_for(literal):
                                    hits.append(
                                        Hit(
                                            page=page_index,
                                            rect=_expand(found),
                                            source="preset",
                                            preset=pid,
                                            literal=literal,
                                        )
                                    )
    return hits, image_pages


def _remove_overlapping_annots(
    doc: fitz.Document, hits: List[Hit]
) -> Tuple[int, List[int]]:
    """删除与命中矩形相交的 FreeText/Stamp 注释（WPS 式文本框不删等于没脱敏）。

    Widget（AcroForm 表单字段）M0 只记录提示不删除（设计 §3.8）。
    返回 (删除注释数, 出现相交 widget 的页号列表[1 基])。
    """
    annots_removed = 0
    widget_pages: Set[int] = set()
    pages = sorted({h.page for h in hits})
    for page_index in pages:
        page = doc[page_index]
        hit_rects = [h.rect for h in hits if h.page == page_index]
        for annot in list(page.annots() or []):
            if annot.type[0] in (fitz.PDF_ANNOT_FREE_TEXT, fitz.PDF_ANNOT_STAMP):
                if any(rect.intersects(annot.rect) for rect in hit_rects):
                    page.delete_annot(annot)
                    annots_removed += 1
        for widget in list(page.widgets() or []):
            if any(rect.intersects(widget.rect) for rect in hit_rects):
                widget_pages.add(page_index + 1)
    return annots_removed, sorted(widget_pages)


def _rasterize_pages(
    doc: fitz.Document, page_indices: Set[int], hit_rects_by_page: Dict[int, List[fitz.Rect]]
) -> fitz.Document:
    """把指定页整页光栅化并在像素上画黑块，其余页原样保留。"""
    zoom = RASTER_DPI / 72.0
    new_doc = fitz.open()
    for i in range(len(doc)):
        page = doc[i]
        prect = page.rect
        if i in page_indices:
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            draw = ImageDraw.Draw(img)
            for rect in hit_rects_by_page.get(i, []):
                px0 = max(0, int(rect.x0 * zoom) - 1)
                px1 = min(pix.width, int(math.ceil(rect.x1 * zoom)) + 1)
                # 命中矩形与 PyMuPDF 全 API 同一视觉坐标系（左上原点 y 向下），
                # 像素坐标同向，直接缩放即可
                py0 = max(0, int(rect.y0 * zoom) - 1)
                py1 = min(pix.height, int(math.ceil(rect.y1 * zoom)) + 1)
                if px1 > px0 and py1 > py0:
                    draw.rectangle([px0, py0, px1, py1], fill=(0, 0, 0))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85)
            new_page = new_doc.new_page(width=prect.width, height=prect.height)
            new_page.insert_image(new_page.rect, stream=buf.getvalue())
        else:
            new_doc.insert_pdf(doc, from_page=i, to_page=i)
    return new_doc


# ---------- 验证通道（独立双引擎，与手术引擎 PyMuPDF 隔离） ----------


def _extract_pdfminer(data: bytes, page_numbers: Optional[List[int]] = None) -> str:
    from pdfminer.high_level import extract_text

    try:
        return extract_text(io.BytesIO(data), page_numbers=page_numbers) or ""
    except Exception:
        # 解析失败按"提取为空"处理——验证断言会因此触发降级（fail closed）
        return ""


def _extract_pdfium(data: bytes) -> List[str]:
    """pypdfium2 逐页提取（index 0 基）。引擎异常页按空串处理（fail closed）。"""
    import pypdfium2 as pdfium

    pages: List[str] = []
    pdf = pdfium.PdfDocument(data)
    try:
        for page in pdf:
            try:
                textpage = page.get_textpage()
                pages.append(textpage.get_text_range() or "")
                textpage.close()
            except Exception:
                pages.append("")
            finally:
                page.close()
    finally:
        pdf.close()
    return pages


def _verify(
    data: bytes,
    literal_targets: Set[str],
    preset_ids: Set[str],
    total_pages: int,
) -> Set[int]:
    """断言全部目标在双引擎提取结果中不存在。

    返回残留页集合（0 基）；空集 = 验证通过。
    pdfminer 逐页提取代价高，先全文断言，仅在失败时才逐页定位残留。
    """
    text_a = _extract_pdfminer(data)
    text_b_pages = _extract_pdfium(data)
    text_b = "\n".join(text_b_pages)

    residue_literals = [t for t in literal_targets if t in text_a or t in text_b]
    residue_presets = [
        p for p in preset_ids if re.search(PRESET_PATTERNS[p], text_a) or re.search(PRESET_PATTERNS[p], text_b)
    ]
    if not residue_literals and not residue_presets:
        return set()

    # 全文有残留 → 逐页定位（pdfminer 逐页 + pdfium 已有逐页）
    residue_pages: Set[int] = set()
    for i in range(total_pages):
        page_a = _extract_pdfminer(data, page_numbers=[i])
        page_b = text_b_pages[i] if i < len(text_b_pages) else ""
        for t in residue_literals:
            if t in page_a or t in page_b:
                residue_pages.add(i)
        for p in residue_presets:
            if re.search(PRESET_PATTERNS[p], page_a) or re.search(PRESET_PATTERNS[p], page_b):
                residue_pages.add(i)
    if not residue_pages:
        # 全文断言失败但逐页定位不到（极端解析差异）——宁可整本降级也不放行
        residue_pages = set(range(total_pages))
    return residue_pages


# ---------- 对外主入口 ----------


def locate(
    pdf_data: bytes, mode: str, rects: List[dict], keywords: Dict[str, list]
) -> Dict[str, object]:
    """只读定位：返回 {matches: [{page(1基), count}], total}，绝不返回原文。"""
    doc = _open_doc(pdf_data)
    try:
        _check_pages(doc, 500)  # locate 放宽页数（只读快），但防止极端大文件
        hits, _ = _collect_hits(doc, mode, rects, keywords)
    finally:
        doc.close()
    per_page: Dict[int, int] = {}
    for h in hits:
        per_page[h.page] = per_page.get(h.page, 0) + 1
    return {
        "matches": [
            {"page": p + 1, "count": c} for p, c in sorted(per_page.items())
        ],
        "total": len(hits),
    }


def redact(
    pdf_data: bytes,
    mode: str,
    rects: List[dict],
    keywords: Dict[str, list],
    deep_clean: bool = False,
) -> Tuple[bytes, Dict[str, object]]:
    """真删除主流程：定位 → 注释清理 → apply_redactions → 扫描页光栅化 → 双引擎验证。

    返回 (输出 PDF bytes, report dict)。验证不通过绝不返回文件。
    deep_clean（M1 深度清理）M0 接受参数但不实现，见交付报告偏差说明。
    """
    doc = _open_doc(pdf_data)
    try:
        _check_pages(doc, 100)
        hits, image_pages = _collect_hits(doc, mode, rects, keywords)

        if mode == "keywords" and not hits:
            raise PDFProcessingError(
                "未在文件中找到匹配的关键词（扫描件/图片页无法按关键词定位，请改用框选涂黑）"
            )

        # 验证目标采集（手术前）：
        literal_targets: Set[str] = set()
        preset_ids: Set[str] = set()
        for h in hits:
            if h.literal:
                literal_targets.add(h.literal)
            if h.preset:
                preset_ids.add(h.preset)
        # 框选模式：把框内已有文字也纳入验证目标（手术后该区域必须提取为空）
        for h in hits:
            if h.source == "rect":
                clipped = doc[h.page].get_text(clip=h.rect).strip()
                if clipped:
                    literal_targets.add(clipped)

        removed_presets: Dict[str, int] = {}
        removed_custom = 0
        rect_count = 0
        for h in hits:
            if h.source == "preset" and h.preset:
                removed_presets[h.preset] = removed_presets.get(h.preset, 0) + 1
            elif h.source == "keyword":
                removed_custom += 1
            elif h.source == "rect":
                rect_count += 1

        annots_removed, widget_pages = _remove_overlapping_annots(doc, hits)

        for h in hits:
            page = doc[h.page]
            clamped = h.rect & page.rect
            if not clamped.is_empty:
                page.add_redact_annot(clamped, fill=(0, 0, 0))
        if hits:
            applied_pages = sorted({h.page for h in hits})
            for page_index in applied_pages:
                doc[page_index].apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS)

        hit_rects_by_page: Dict[int, List[fitz.Rect]] = {}
        for h in hits:
            hit_rects_by_page.setdefault(h.page, []).append(h.rect)

        # 扫描件/图片页（有框选命中的）走像素涂黑
        rasterize_pages: Set[int] = {
            i for i in image_pages if i in hit_rects_by_page
        }
        if rasterize_pages:
            doc = _rasterize_pages(doc, rasterize_pages, hit_rects_by_page)

        out_bytes = doc.tobytes(deflate=True, garbage=3)
        total_pages = len(doc)

        # 验证通道：不通过 → 定位残留页整页光栅化 → 重验；仍失败绝不放行
        residue = _verify(out_bytes, literal_targets, preset_ids, total_pages)
        if residue:
            logger.warning(
                f"redact 首轮验证发现残留页 {sorted(r + 1 for r in residue)}，触发光栅化降级"
            )
            fallback_pages = {i for i in residue}
            doc = _rasterize_pages(doc, fallback_pages, hit_rects_by_page)
            out_bytes = doc.tobytes(deflate=True, garbage=3)
            residue2 = _verify(out_bytes, literal_targets, preset_ids, len(doc))
            if residue2:
                raise PDFProcessingError(
                    "脱敏验证未通过，为保护隐私本次不返回文件。请重试，或分页处理后重试。"
                )
            rasterize_pages |= residue

        report = {
            "removed": {
                "presets": removed_presets,
                "custom": removed_custom,
                "rects": rect_count,
            },
            "rasterizedPages": sorted(p + 1 for p in rasterize_pages),
            "imagePages": sorted(p + 1 for p in image_pages),
            "annotsRemoved": annots_removed,
            "widgetWarningPages": widget_pages,
            "verified": True,
        }
        logger.info(
            f"redact 完成: mode={mode} hits={len(hits)} rasterized={sorted(rasterize_pages)} "
            f"annots_removed={annots_removed} verified=True"
        )
        return out_bytes, report
    finally:
        try:
            doc.close()
        except Exception:
            pass


def encode_report(report: Dict[str, object]) -> str:
    """报告 → base64url（X-Redact-Report 头，≤6KB；超限返回空串由前端降级通用文案）。"""
    raw = json.dumps(report, ensure_ascii=False).encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return encoded if len(encoded) <= 8192 else ""
