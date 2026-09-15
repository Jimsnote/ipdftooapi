"""PDF 涂黑/脱敏（redact）测试集。

覆盖范围（对应 docs/2026-09-14_231000-redact-pdf-true-redaction-design.md §10）：
- CJK 命中/非命中同页黄金断言（防误删 + 双引擎互验）
- 身份证/手机/邮箱正则预设命中与干扰项不误删
- FreeText/Stamp 注释清理、Widget 相交警告
- 扫描页（图片无文本层）像素涂黑 + 混合重建
- 旋转页、加密拒绝、页数上限、大小上限（路由层）
- 双引擎（pdfminer.six + pypdfium2）互验
- 验证降级路径触发 / 验证失败绝不返回文件
- locate 只读定位、报告编码、参数校验

约定：样本全部用 fitz 直造（对提示词 reportlab 的偏差见交付报告）；
测试内禁止 shutil.rmtree，临时目录交给系统 / pytest --basetemp 管理。
"""
import base64
import io
import json
import os
import re
import tempfile

import fitz
import pytest
from PIL import Image, ImageDraw

from app.core.exceptions import PDFProcessingError
from app.services import redactor
from app.services.redactor import (
    EMAIL_PATTERN,
    ID_PATTERN,
    PHONE_PATTERN,
    encode_report,
    locate,
    parse_keywords,
    parse_rects,
    redact,
)

# ---------- 双引擎提取（与手术引擎 PyMuPDF 隔离，测试独立复现验证逻辑） ----------


def _extract_both(data: bytes):
    """返回 (pdfminer 全文, pypdfium2 全文)。"""
    from pdfminer.high_level import extract_text
    import pypdfium2 as pdfium

    text_a = extract_text(io.BytesIO(data)) or ""
    pdf = pdfium.PdfDocument(data)
    pages = []
    try:
        for page in pdf:
            try:
                tp = page.get_textpage()
                pages.append(tp.get_text_range() or "")
                tp.close()
            except Exception:
                pages.append("")
            finally:
                page.close()
    finally:
        pdf.close()
    return text_a, "\n".join(pages)


def _assert_gone_in_both(data: bytes, literal: str) -> None:
    text_a, text_b = _extract_both(data)
    assert literal not in text_a, f"pdfminer 仍能提取到敏感词: {literal!r}"
    assert literal not in text_b, f"pypdfium2 仍能提取到敏感词: {literal!r}"


def _assert_gone_ignoring_ws(data: bytes, literal: str) -> None:
    """宽松消失断言：剥掉全部空白后比对（旋转页逐字符提取场景的漏删检测）。"""
    text_a, text_b = _extract_both(data)
    squash = lambda s: re.sub(r"\s+", "", s)
    assert literal not in squash(text_a), f"pdfminer 仍能提取到敏感词: {literal!r}"
    assert literal not in squash(text_b), f"pypdfium2 仍能提取到敏感词: {literal!r}"


def _assert_kept_ignoring_ws(data: bytes, literal: str) -> None:
    """宽松保留断言：剥掉全部空白后比对（pdfminer 对旋转页逐字符提取，字符间插 \n）。"""
    text_a, text_b = _extract_both(data)
    squash = lambda s: re.sub(r"\s+", "", s)
    assert literal in squash(text_a), f"pdfminer 丢失了应保留文本: {literal!r}"
    assert literal in squash(text_b), f"pypdfium2 丢失了应保留文本: {literal!r}"


def _assert_kept_in_both(data: bytes, literal: str) -> None:
    text_a, text_b = _extract_both(data)
    assert literal in text_a, f"pdfminer 丢失了应保留文本: {literal!r}"
    assert literal in text_b, f"pypdfium2 丢失了应保留文本: {literal!r}"


# ---------- 样本构造 ----------

ID_SAMPLE = "110101199003077757"  # 110101 + 19900307 + 7757，格式合规（不校验校验位）
PHONE_SAMPLE = "13812345678"
EMAIL_SAMPLE = "zhangsan@example.com"


def _text_page(page, lines, y_start=100, dy=30):
    """在页面上逐行插入文本（每次独立 insert_text → 独立 span，便于正则匹配）。"""
    y = y_start
    for line in lines:
        page.insert_text(fitz.Point(72, y), line, fontname="china-s", fontsize=12)
        y += dy


def _make_keywords_pdf() -> bytes:
    """黄金断言样本：CJK 命中词与非命中正文同页。"""
    doc = fitz.open()
    page = doc.new_page(width=595.27, height=841.89)
    _text_page(
        page,
        [
            "本合同由张三丰与李四共同签署。",
            "普通正文内容不得删除。",
            "此区域以外的信息应完整保留。",
        ],
    )
    data = doc.tobytes()
    doc.close()
    return data


def _make_presets_pdf(num_extra_pages: int = 0) -> bytes:
    """正则预设样本：身份证/手机/邮箱 + 干扰项（不合规号码、坏邮箱）。"""
    doc = fitz.open()
    page = doc.new_page(width=595.27, height=841.89)
    _text_page(
        page,
        [
            f"身份证号 {ID_SAMPLE} 请核对",
            f"联系电话 {PHONE_SAMPLE}",
            f"邮箱 {EMAIL_SAMPLE}",
            "干扰项1 112233445566778",
            "干扰项2 12345678901",
            "干扰项3 bad-mail-at-nowhere",
        ],
    )
    for _ in range(num_extra_pages):
        p = doc.new_page(width=595.27, height=841.89)
        _text_page(p, [f"第二处电话 {PHONE_SAMPLE}"])
    data = doc.tobytes()
    doc.close()
    return data


def _make_freetext_pdf() -> bytes:
    """FreeText 注释承载敏感内容的样本（注释不在页面内容流，须相交删除）。"""
    doc = fitz.open()
    page = doc.new_page(width=595.27, height=841.89)
    page.insert_text(
        fitz.Point(72, 100), "普通正文内容在这里。", fontname="china-s", fontsize=12
    )
    page.add_freetext_annot(
        fitz.Rect(300, 300, 460, 340), "SECRET-ANNOT-TEXT", fontsize=12
    )
    data = doc.tobytes()
    doc.close()
    return data


def _make_scanned_pdf() -> bytes:
    """两页：文本页 + 图片扫描页（无文本层）。"""
    doc = fitz.open()
    p0 = doc.new_page(width=595.27, height=841.89)
    _text_page(p0, ["第一页普通文本内容较多一些。"])

    # PIL 造一张"扫描件"：白底 + 数个黑块模拟文字（无文本层）
    img = Image.new("RGB", (1190, 1684), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    for i in range(12):
        y = 150 + i * 100
        draw.rectangle([100, y, 900, y + 40], fill=(20, 20, 20))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    p1 = doc.new_page(width=595.27, height=841.89)
    p1.insert_image(p1.rect, stream=buf.getvalue())
    data = doc.tobytes()
    doc.close()
    return data


def _make_rotated_pdf() -> bytes:
    """90 度旋转页（文本须 ≥5 字符，避免被误判为图片页）。"""
    doc = fitz.open()
    page = doc.new_page(width=595.27, height=841.89)
    _text_page(page, ["这一行是旋转页普通正文文本。", "这里藏着敏感词汇需要删除。"])
    page.set_rotation(90)
    data = doc.tobytes()
    doc.close()
    return data


def _make_encrypted_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=595.27, height=841.89)
    _text_page(page, ["加密文件不应被涂黑处理。"])
    data = doc.tobytes(
        encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner-secret", user_pw="user-pw"
    )
    doc.close()
    return data


def _make_widget_pdf() -> bytes:
    doc = fitz.open()
    page = doc.new_page(width=595.27, height=841.89)
    page.insert_text(
        fitz.Point(72, 100), "表单字段相交警告测试页面。", fontname="china-s", fontsize=12
    )
    w = fitz.Widget()
    w.field_name = "secret_field"
    w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w.rect = fitz.Rect(100, 200, 260, 230)
    w.field_value = " sensitivedata "
    page.add_widget(w)
    data = doc.tobytes()
    doc.close()
    return data


def _make_multipage_pdf(n: int) -> bytes:
    doc = fitz.open()
    for i in range(n):
        page = doc.new_page(width=595.27, height=841.89)
        page.insert_text(fitz.Point(72, 400), f"page {i + 1} body text", fontsize=12)
    data = doc.tobytes()
    doc.close()
    return data


# ---------- 参数解析（纯函数） ----------


class TestParsers:
    def test_parse_rects_ok(self):
        cleaned = parse_rects([{"page": 1, "x": 10, "y": 20, "w": 100, "h": 30}])
        assert cleaned == [{"page": 1, "x": 10.0, "y": 20.0, "w": 100.0, "h": 30.0}]

    @pytest.mark.parametrize(
        "bad",
        [
            "not-a-list",
            [{"page": 0, "x": 1, "y": 1, "w": 1, "h": 1}],  # page 必须 1 基
            [{"page": 1, "x": 1, "y": 1, "w": 0, "h": 1}],  # 宽必须 > 0
            [{"page": 1, "x": 1, "y": 1, "w": 1, "h": -2}],  # 高必须 > 0
            [{"page": 1, "x": -5, "y": 1, "w": 1, "h": 1}],  # 坐标不能为负
            [{"page": 1, "x": 1, "y": 1, "w": 1}],  # 缺字段
            [{"page": 1, "x": float("nan"), "y": 1, "w": 1, "h": 1}],  # NaN
            [{"page": 1, "x": 1, "y": 1, "w": float("inf"), "h": 1}],  # Infinity
            [{"page": 1, "x": float("-inf"), "y": 1, "w": 1, "h": 1}],  # -Infinity
        ],
    )
    def test_parse_rects_rejects(self, bad):
        with pytest.raises(PDFProcessingError):
            parse_rects(bad)

    def test_parse_keywords_ok(self):
        obj = parse_keywords({"presets": ["id", "phone"], "custom": [" 张三 ", ""]})
        assert obj == {"presets": ["id", "phone"], "custom": ["张三"]}

    def test_parse_keywords_unknown_preset(self):
        with pytest.raises(PDFProcessingError):
            parse_keywords({"presets": ["passport"], "custom": []})

    def test_parse_keywords_too_long(self):
        with pytest.raises(PDFProcessingError):
            parse_keywords({"presets": [], "custom": ["x" * 101]})


# ---------- 关键词模式：黄金断言 ----------


class TestKeywordsGolden:
    def test_cjk_hit_removed_and_body_kept(self):
        """同页 CJK 命中/非命中黄金断言 + 双引擎互验。"""
        data = _make_keywords_pdf()
        out, report = redact(
            data, "keywords", [], {"presets": [], "custom": ["张三丰"]}
        )
        _assert_gone_in_both(out, "张三丰")
        _assert_kept_in_both(out, "普通正文内容")
        _assert_kept_in_both(out, "李四")
        assert report["removed"]["custom"] == 1
        assert report["verified"] is True

    def test_preset_id_phone_email_removed_interference_kept(self):
        """三个正则预设命中删除，干扰项（不合规号码/坏邮箱）必须保留。"""
        data = _make_presets_pdf()
        out, report = redact(
            data,
            "keywords",
            [],
            {"presets": ["id", "phone", "email"], "custom": []},
        )
        _assert_gone_in_both(out, ID_SAMPLE)
        _assert_gone_in_both(out, PHONE_SAMPLE)
        _assert_gone_in_both(out, EMAIL_SAMPLE)
        # 干扰项保留
        _assert_kept_in_both(out, "112233445566778")
        _assert_kept_in_both(out, "12345678901")
        _assert_kept_in_both(out, "bad-mail-at-nowhere")
        # 输出全文不再匹配任一预设正则（双引擎）
        text_a, text_b = _extract_both(out)
        for pat in (ID_PATTERN, PHONE_PATTERN, EMAIL_PATTERN):
            assert not re.search(pat, text_a), f"pdfminer 输出仍有预设匹配: {pat}"
            assert not re.search(pat, text_b), f"pypdfium2 输出仍有预设匹配: {pat}"
        assert report["removed"]["presets"] == {"id": 1, "phone": 1, "email": 1}

    def test_mixed_presets_and_custom(self):
        data = _make_presets_pdf(num_extra_pages=1)
        out, report = redact(
            data,
            "keywords",
            [],
            {"presets": ["phone"], "custom": ["第二处"]},
        )
        _assert_gone_in_both(out, PHONE_SAMPLE)
        _assert_kept_in_both(out, ID_SAMPLE)  # 未选 id 预设 → 身份证保留
        assert report["removed"]["custom"] == 1

    def test_no_match_raises(self):
        data = _make_keywords_pdf()
        with pytest.raises(PDFProcessingError, match="未在文件中找到匹配"):
            redact(data, "keywords", [], {"presets": [], "custom": ["不存在的词"]})


# ---------- 框选模式 ----------


class TestRectsMode:
    def test_rect_redaction_golden(self):
        """框选模式：框内文字删除、框外保留。"""
        doc = fitz.open()
        page = doc.new_page(width=595.27, height=841.89)
        page.insert_text(fitz.Point(72, 100), "AAA-line-to-erase", fontsize=12)
        page.insert_text(fitz.Point(72, 160), "BBB-line-to-keep", fontsize=12)
        data = doc.tobytes()
        doc.close()
        # 框住第一行（用户坐标 1 基页码）
        out, report = redact(
            data, "rects", [{"page": 1, "x": 60, "y": 88, "w": 200, "h": 24}], {}
        )
        _assert_gone_in_both(out, "AAA-line-to-erase")
        _assert_kept_in_both(out, "BBB-line-to-keep")
        assert report["removed"]["rects"] == 1

    def test_rect_redaction_with_cropbox_offset(self):
        """CropBox 原点偏移页：前端送 cropbox 相对视觉坐标，后端原样使用。

        坐标约定（2026-09-15 定版）：前端 = css 像素 / pdf.js viewport.scale，
        即左上原点、y 向下、cropbox 相对、单位 pt——与 PyMuPDF 全 API 同系。
        历史：v1 曾按"绝对 y-up 坐标+偏移校正"实现（855a547），实为错误理论
        （convertToPdfPoint 的 y-up 输出按 y-down 解释会整页垂直镜像），
        已随前端改送视觉坐标一并修正。
        """
        doc = fitz.open()
        page = doc.new_page(width=595.27, height=841.89)
        page.insert_text(fitz.Point(72, 100), "AAA-line-to-erase", fontsize=12)
        page.insert_text(fitz.Point(72, 160), "BBB-line-to-keep", fontsize=12)
        page.set_cropbox(fitz.Rect(50, 30, 595.27, 841.89))
        data = doc.tobytes()
        doc.close()
        # set_cropbox 后 AAA 相对 bbox = y 57.1~73.6（上移 30）；前端按视觉
        # 位置框选（css/scale），即相对坐标 55~76
        out, report = redact(
            data, "rects", [{"page": 1, "x": 20, "y": 55, "w": 220, "h": 21}], {}
        )
        _assert_gone_in_both(out, "AAA-line-to-erase")
        _assert_kept_in_both(out, "BBB-line-to-keep")
        assert report["removed"]["rects"] == 1
        # 黑块相对坐标应罩住 AAA（55~76 外扩 1pt → 54~77）
        doc = fitz.open(stream=out, filetype="pdf")
        try:
            blacks = [
                d["rect"]
                for d in doc[0].get_drawings()
                if d.get("fill") and all(c < 0.1 for c in d["fill"]) and d["rect"].width > 30
            ]
            assert blacks, "未找到黑块"
            r = blacks[0]
            assert 50 <= r.y0 <= 60 and 70 <= r.y1 <= 82, f"黑块位置异常: {r}"
        finally:
            doc.close()

    def test_rect_redaction_rotated_page(self):
        """/Rotate 90 页：前端 viewport 自带旋转处理，css/scale 即视觉坐标；
        PyMuPDF search_for/add_redact_annot 同为视觉坐标，必须精确命中。"""
        doc = fitz.open()
        page = doc.new_page(width=841.89, height=595.27)
        page.insert_text(fitz.Point(72, 100), "ROT-AAA-to-erase", fontsize=12)
        page.insert_text(fitz.Point(72, 160), "ROT-BBB-to-keep", fontsize=12)
        page.set_rotation(90)
        data = doc.tobytes()
        # search_for 视觉坐标（实测 ROT-AAA = 72, 87.1, 173.4, 103.6），
        # 模拟前端按视觉位置框选
        probe = fitz.open(stream=data, filetype="pdf")
        try:
            vr = probe[0].search_for("ROT-AAA-to-erase")[0]
        finally:
            probe.close()
        out, report = redact(
            data,
            "rects",
            [
                {
                    "page": 1,
                    "x": vr.x0 - 2,
                    "y": vr.y0 - 2,
                    "w": vr.width + 4,
                    "h": vr.height + 4,
                }
            ],
            {},
        )
        # pdfminer 对旋转页逐字符提取插 \n → 用剥空白断言
        _assert_gone_ignoring_ws(out, "ROT-AAA-to-erase")
        _assert_kept_ignoring_ws(out, "ROT-BBB-to-keep")
        assert report["removed"]["rects"] == 1

    def test_scanned_page_pixel_black_and_text_page_kept(self):
        """扫描页框选 → 像素涂黑；同文件文本页原样保留（混合重建）。"""
        data = _make_scanned_pdf()
        out, report = redact(
            data, "rects", [{"page": 2, "x": 80, "y": 100, "w": 200, "h": 60}], {}
        )
        assert report["rasterizedPages"] == [2]
        assert report["imagePages"] == [2]
        # 文本页没被破坏
        _assert_kept_in_both(out, "第一页普通文本内容较多一些")
        # 像素断言：输出第 2 页在框选中心必须是黑色
        doc = fitz.open(stream=out, filetype="pdf")
        try:
            page = doc[1]
            assert page.get_text().strip() == ""  # 图片页无文本层
            zoom = 2.0
            pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            # 框中心 (180, 130)——视觉坐标（左上原点）→ 像素坐标同向直接缩放
            px = int(180 * zoom)
            py = int(130 * zoom)
            r, g, b = pix.pixel(px, py)[:3]
            assert (r, g, b) == (0, 0, 0), f"框选中心不是黑色: {(r, g, b)}"
        finally:
            doc.close()

    def test_freetext_annot_removed(self):
        """FreeText 注释（WPS 式文本框）与框选相交 → 必须删除。"""
        data = _make_freetext_pdf()
        out, report = redact(
            data, "rects", [{"page": 1, "x": 290, "y": 290, "w": 180, "h": 60}], {}
        )
        assert report["annotsRemoved"] == 1
        _assert_gone_in_both(out, "SECRET-ANNOT-TEXT")
        _assert_kept_in_both(out, "普通正文内容在这里")

    def test_widget_intersection_reported_not_deleted(self):
        """相交 Widget M0 只警告不删除（设计 §3.8）。"""
        data = _make_widget_pdf()
        out, report = redact(
            data, "rects", [{"page": 1, "x": 90, "y": 190, "w": 200, "h": 60}], {}
        )
        assert report["widgetWarningPages"] == [1]
        assert report["annotsRemoved"] == 0

    def test_all_annot_types_with_contents_removed(self):
        """对抗审查：Square/Text 等注释的 /Contents 藏原文 → 相交即删。

        注释内容不进文本验证通道，漏删 = 永久泄露（2026-09-15 实测）。
        """
        doc = fitz.open()
        page = doc.new_page(width=595.27, height=841.89)
        page.insert_text(fitz.Point(72, 100), "REDACT-THIS-LINE", fontsize=12)
        page.insert_text(fitz.Point(72, 160), "keep-me-line", fontsize=12)
        a1 = page.add_rect_annot(fitz.Rect(60, 88, 280, 106))
        doc.xref_set_key(a1.xref, "Contents", "(note: TOP-SECRET-CONTENT-HERE)")
        a1.update()
        a2 = page.add_text_annot(fitz.Point(250, 100), "sticky SECRET-NOTE-X", icon="Comment")
        a2.update()
        # 不相交的注释必须保留
        a3 = page.add_text_annot(fitz.Point(300, 700), "unrelated note", icon="Comment")
        a3.update()
        data = doc.tobytes()
        doc.close()
        out, report = redact(
            data, "rects", [{"page": 1, "x": 60, "y": 88, "w": 220, "h": 24}], {}
        )
        assert report["annotsRemoved"] == 2
        doc = fitz.open(stream=out, filetype="pdf")
        try:
            survivors = list(doc[0].annots() or [])
            joined = "\n".join(doc.xref_object(a.xref) for a in survivors)
            assert "TOP-SECRET-CONTENT-HERE" not in joined
            assert "SECRET-NOTE-X" not in joined
            assert "unrelated note" in joined
        finally:
            doc.close()

    def test_outline_bookmark_scrubbed(self):
        """对抗审查：书签/目录标题残留被涂黑文字 → 必须清除，无关书签保留。"""
        doc = fitz.open()
        page = doc.new_page(width=595.27, height=841.89)
        page.insert_text(fitz.Point(72, 100), "TOP-SECRET-OUTLINE-TEXT", fontsize=12)
        page.insert_text(fitz.Point(72, 160), "normal-content-here", fontsize=12)
        doc.set_toc(
            [
                [1, "Chapter: TOP-SECRET-OUTLINE-TEXT", 1],
                [1, "Appendix: normal-content-here", 1],
            ]
        )
        data = doc.tobytes()
        doc.close()
        out, _ = redact(
            data, "rects", [{"page": 1, "x": 60, "y": 88, "w": 220, "h": 24}], {}
        )
        doc = fitz.open(stream=out, filetype="pdf")
        try:
            toc = doc.get_toc()
            titles = [t[1] for t in toc]
            assert not any("TOP-SECRET" in t for t in titles), f"书签泄露: {titles}"
            assert any("Appendix" in t for t in titles), f"误删无关书签: {titles}"
        finally:
            doc.close()

    def test_outline_nested_parent_scrub_keeps_valid_levels(self):
        """对抗审查：删除层级书签的敏感父节点后，子节点层级须规一化不报错。"""
        doc = fitz.open()
        page = doc.new_page(width=595.27, height=841.89)
        page.insert_text(fitz.Point(72, 100), "SECRET-CHAPTER-NAME", fontsize=12)
        page.insert_text(fitz.Point(72, 160), "plain-body-text-here", fontsize=12)
        doc.set_toc(
            [
                [1, "SECRET-CHAPTER-NAME", 1],
                [2, "section alpha", 1],
                [2, "section beta", 1],
                [3, "subsection gamma", 1],
            ]
        )
        data = doc.tobytes()
        doc.close()
        out, _ = redact(
            data, "rects", [{"page": 1, "x": 60, "y": 88, "w": 220, "h": 24}], {}
        )
        doc = fitz.open(stream=out, filetype="pdf")
        try:
            toc = doc.get_toc()
            assert [t[1] for t in toc] == [
                "section alpha",
                "section beta",
                "subsection gamma",
            ]
            levels = [t[0] for t in toc]
            assert levels == [1, 1, 2], f"层级未规一化: {levels}"
        finally:
            doc.close()

    def test_rects_zero_hits_fails_closed(self):
        """对抗审查：框选页码超出文件页数 → 必须报错，禁止静默返回未涂黑原文件。"""
        doc = fitz.open()
        page = doc.new_page(width=595.27, height=841.89)
        page.insert_text(fitz.Point(72, 100), "sensitive-data-here", fontsize=12)
        data = doc.tobytes()
        doc.close()
        with pytest.raises(PDFProcessingError):
            redact(
                data, "rects", [{"page": 9, "x": 60, "y": 88, "w": 200, "h": 24}], {}
            )

    def test_verify_tolerates_whitespace_variation(self, monkeypatch):
        """对抗审查：双引擎空白/行序差异不得造成假残留 → 误触发光栅化降级。"""
        data = _make_keywords_pdf()
        out, report = redact(
            data, "rects", [{"page": 1, "x": 60, "y": 88, "w": 200, "h": 24}], {}
        )
        assert report["rasterizedPages"] == []
        # 模拟 pdfminer 对输出的提取与 fitz clip 存在空白差异
        import app.services.redactor as R

        real_extract = R._extract_pdfminer

        def spaced(data_bytes, page_numbers=None):
            text = real_extract(data_bytes, page_numbers=page_numbers)
            return " ".join(text)  # 每个字符间插空格

        monkeypatch.setattr(R, "_extract_pdfminer", spaced)
        residue = R._verify(
            out,
            {"AAA-line-to-erase"},
            set(),
            len(fitz.open(stream=out, filetype="pdf")),
        )
        assert residue == set(), f"空白差异被误判为残留: {residue}"

    def test_empty_rects_raises_at_route_level(self):
        # 路由层校验；service 层空 rects 框选 = 零命中，仍正常返回
        data = _make_keywords_pdf()
        out, report = redact(data, "rects", [], {})
        assert report["removed"]["rects"] == 0


# ---------- 特殊场景 ----------


class TestEdgeCases:
    def test_rotated_page(self):
        """旋转页关键词命中删除，且不误删普通文本。"""
        data = _make_rotated_pdf()
        out, report = redact(
            data, "keywords", [], {"presets": [], "custom": ["敏感词汇"]}
        )
        _assert_gone_ignoring_ws(out, "敏感词汇")
        _assert_kept_ignoring_ws(out, "旋转页普通正文文本")

    def test_encrypted_rejected(self):
        data = _make_encrypted_pdf()
        with pytest.raises(PDFProcessingError, match="加密"):
            redact(data, "keywords", [], {"presets": [], "custom": ["加密文件"]})

    def test_page_limit_100(self):
        data = _make_multipage_pdf(101)
        with pytest.raises(PDFProcessingError, match="页数超过上限"):
            redact(data, "rects", [{"page": 1, "x": 1, "y": 1, "w": 10, "h": 10}], {})

    def test_corrupt_pdf_rejected(self):
        with pytest.raises(PDFProcessingError, match="损坏"):
            redact(b"this is not a pdf", "rects", [], {})

    def test_locate_service(self):
        data = _make_presets_pdf(num_extra_pages=2)  # 3 页，每页 1 个手机号
        result = locate(
            data, "keywords", [], {"presets": ["phone"], "custom": []}
        )
        assert result["total"] == 3
        assert result["matches"] == [
            {"page": 1, "count": 1},
            {"page": 2, "count": 1},
            {"page": 3, "count": 1},
        ]
        # 不回显原文：结果里没有字面量
        assert json.dumps(result, ensure_ascii=False).find(PHONE_SAMPLE) == -1

    def test_locate_500_page_limit(self):
        """locate 页数放宽到 500：100 页能过 locate（redact 会拒）。"""
        data = _make_multipage_pdf(100)
        result = locate(data, "keywords", [], {"presets": [], "custom": ["不存在词"]})
        assert result["total"] == 0

    def test_encode_report_roundtrip(self):
        report = {"removed": {"presets": {"id": 2}, "custom": 0, "rects": 0}, "verified": True}
        encoded = encode_report(report)
        assert encoded and "=" not in encoded  # base64url 无 padding
        decoded = json.loads(base64.urlsafe_b64decode(encoded + "=="))
        assert decoded["verified"] is True

    def test_encode_report_oversize_returns_empty(self):
        huge = {"big": "x" * 20000}
        assert encode_report(huge) == ""


# ---------- 验证降级与 fail-closed ----------


class TestVerifyFallback:
    def test_fallback_rasterize_on_residue(self, monkeypatch):
        """首轮验证发现残留 → 整页光栅化 → 重验通过。"""
        data = _make_keywords_pdf()
        calls = {"n": 0}
        real_verify = redactor._verify

        def fake_verify(pdf_bytes, literals, presets, total):
            calls["n"] += 1
            return {0} if calls["n"] == 1 else set()

        monkeypatch.setattr(redactor, "_verify", fake_verify)
        out, report = redact(
            data, "keywords", [], {"presets": [], "custom": ["张三丰"]}
        )
        assert calls["n"] == 2
        assert 1 in report["rasterizedPages"]
        assert report["verified"] is True
        # 光栅化后文本层应彻底消失
        doc = fitz.open(stream=out, filetype="pdf")
        try:
            assert doc[0].get_text().strip() == ""
        finally:
            doc.close()

    def test_verify_failure_never_returns_file(self, monkeypatch):
        """验证两次都失败 → 绝不返回文件（fail closed）。"""
        data = _make_keywords_pdf()
        monkeypatch.setattr(redactor, "_verify", lambda *a, **k: {0})
        with pytest.raises(PDFProcessingError, match="验证未通过"):
            redact(data, "keywords", [], {"presets": [], "custom": ["张三丰"]})


# ---------- 路由层（TestClient） ----------


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c


class TestRoutes:
    def test_redact_endpoint_ok(self, client):
        data = _make_keywords_pdf()
        resp = client.post(
            "/api/v1/redact",
            files={"file": ("合同.pdf", data, "application/pdf")},
            data={
                "mode": "keywords",
                "keywords": json.dumps({"presets": [], "custom": ["张三丰"]}),
            },
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/pdf")
        assert "filename*" in resp.headers.get("content-disposition", "")  # 中文文件名
        # 报告头可解码
        report = json.loads(
            base64.urlsafe_b64decode(resp.headers["X-Redact-Report"] + "==")
        )
        assert report["verified"] is True
        # 输出内容黄金断言
        _assert_gone_in_both(resp.content, "张三丰")
        _assert_kept_in_both(resp.content, "普通正文内容")

    def test_locate_endpoint_ok(self, client):
        data = _make_presets_pdf()
        resp = client.post(
            "/api/v1/redact/locate",
            files={"file": ("a.pdf", data, "application/pdf")},
            data={"keywords": json.dumps({"presets": ["phone"], "custom": []})},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["matches"] == [{"page": 1, "count": 1}]
        assert PHONE_SAMPLE not in resp.text

    def test_route_rejects_empty_keywords(self, client):
        data = _make_keywords_pdf()
        resp = client.post(
            "/api/v1/redact",
            files={"file": ("a.pdf", data, "application/pdf")},
            data={"mode": "keywords", "keywords": "{}"},
        )
        assert resp.status_code == 422

    def test_route_rejects_bad_mode(self, client):
        data = _make_keywords_pdf()
        resp = client.post(
            "/api/v1/redact",
            files={"file": ("a.pdf", data, "application/pdf")},
            data={"mode": "magic"},
        )
        assert resp.status_code == 422

    def test_route_rejects_non_pdf(self, client):
        resp = client.post(
            "/api/v1/redact",
            files={"file": ("a.txt", b"hello", "text/plain")},
            data={"mode": "rects", "rects": json.dumps([{"page": 1, "x": 1, "y": 1, "w": 5, "h": 5}])},
        )
        assert resp.status_code == 415

    def test_route_size_limit(self, client, monkeypatch):
        """20MB 上限：monkeypatch 调小阈值模拟超限（不真造 20MB 文件）。"""
        from app.config import settings

        monkeypatch.setattr(settings, "REDACT_MAX_SIZE", 100)
        data = _make_keywords_pdf()  # > 100 字节
        resp = client.post(
            "/api/v1/redact",
            files={"file": ("a.pdf", data, "application/pdf")},
            data={"mode": "rects", "rects": json.dumps([{"page": 1, "x": 1, "y": 1, "w": 5, "h": 5}])},
        )
        assert resp.status_code == 413

    def test_locate_no_match_ok(self, client):
        data = _make_keywords_pdf()
        resp = client.post(
            "/api/v1/redact/locate",
            files={"file": ("a.pdf", data, "application/pdf")},
            data={"keywords": json.dumps({"presets": [], "custom": ["不存在的词"]})},
        )
        assert resp.status_code == 200
        assert resp.json() == {"matches": [], "total": 0}
