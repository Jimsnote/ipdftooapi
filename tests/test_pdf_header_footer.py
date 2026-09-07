import os
import tempfile

import fitz
import pytest
from pypdf import PdfWriter

from app.services.pdf_header_footer import PDFHeaderFooter


def _make_pdf(path: str, num_pages: int, with_content: bool = True) -> None:
    """空白 PDF 渲染全白无像素，断言必须画可见内容（MEMORY 教训）。"""
    doc = fitz.open()
    for i in range(num_pages):
        page = doc.new_page(width=595.27, height=841.89)  # A4
        if with_content:
            page.insert_text(fitz.Point(72, 400), f"body page {i + 1}", fontsize=12)
    doc.save(path)
    doc.close()


def _page_text(pdf_path: str, page_index: int = 0) -> str:
    doc = fitz.open(pdf_path)
    try:
        return doc[page_index].get_text()
    finally:
        doc.close()


class TestHeaderFooter:
    def setup_method(self):
        self.d = tempfile.mkdtemp()
        self.input = os.path.join(self.d, "input.pdf")
        _make_pdf(self.input, 3)

    def test_header_chinese_text_applied(self):
        out = os.path.join(self.d, "out.pdf")
        info = PDFHeaderFooter(self.input).apply(
            out,
            header_text="测试软件 V1.0",
            header_position="header-center",
            page_number_position="none",
        )
        assert info["headers_applied"] == 3
        # Noto Serif SC 提取时空格可能映射为 \xa0，分开断言
        text = _page_text(out).replace("\xa0", " ")
        assert "测试软件 V1.0" in text

    def test_page_number_right_top(self):
        out = os.path.join(self.d, "out.pdf")
        info = PDFHeaderFooter(self.input).apply(
            out,
            page_number_position="header-right",
            page_number_format="zh_total",
            page_number_start=1,
        )
        assert info["numbers_applied"] == 3
        # 页码坐标在页面右上区域（y < 100, x > 宽度一半）
        doc = fitz.open(out)
        try:
            page = doc[0]
            words = page.get_text("words")  # (x0,y0,x1,y1,word,...)
            pn = [w for w in words if w[4].startswith("第")]
            assert pn, "未找到页码文本"
            x0, y0, x1, y1 = pn[0][:4]
            assert y0 < 100, f"页码不在页眉区: y0={y0}"
            assert x0 > page.rect.width / 2, f"页码不在右侧: x0={x0}"
        finally:
            doc.close()

    def test_page_number_start_offset(self):
        out = os.path.join(self.d, "out.pdf")
        PDFHeaderFooter(self.input).apply(
            out, page_number_position="footer-center",
            page_number_format="plain", page_number_start=5,
        )
        # 第 1 页显示 5，第 3 页显示 7
        assert "5" in _page_text(out, 0)
        assert "7" in _page_text(out, 2)

    def test_variables_rendered(self):
        out = os.path.join(self.d, "out.pdf")
        PDFHeaderFooter(self.input).apply(
            out,
            header_text="{filename} 第{page}页/共{pages}页",
            header_position="header-left",
            page_number_position="none",
        )
        text = _page_text(out, 0)
        assert "input" in text
        assert "第1页/共3页" in text

    def test_footer_position_and_format(self):
        out = os.path.join(self.d, "out.pdf")
        PDFHeaderFooter(self.input).apply(
            out,
            footer_text="某某科技有限公司",
            footer_position="footer-center",
            page_number_position="none",
        )
        doc = fitz.open(out)
        try:
            page = doc[0]
            words = page.get_text("words")
            fn = [w for w in words if "科技" in w[4]]
            assert fn, "未找到页脚文本"
            assert fn[0][1] > page.rect.height - 100, "页脚不在底部区域"
        finally:
            doc.close()

    def test_apply_from_page_skips_earlier(self):
        out = os.path.join(self.d, "out.pdf")
        info = PDFHeaderFooter(self.input).apply(
            out,
            header_text="ONLY_FROM_2",
            page_number_position="none",
            apply_from_page=2,
        )
        assert info["headers_applied"] == 2
        assert "ONLY_FROM_2" not in _page_text(out, 0)
        assert "ONLY_FROM_2" in _page_text(out, 1)

    def test_color_applied(self):
        out = os.path.join(self.d, "out.pdf")
        PDFHeaderFooter(self.input).apply(
            out, header_text="RED", header_color="#FF0000",
            page_number_position="none",
        )
        doc = fitz.open(out)
        try:
            spans = []
            for block in doc[0].get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        if span["text"] == "RED":
                            spans.append(span)
            assert spans, "未找到页眉文本"
            c = spans[0]["color"]  # pymupdf 返回单个 int（sRGB）
            r, g, b = (c >> 16) & 255, (c >> 8) & 255, c & 255
            assert r == 255 and g == 0 and b == 0, f"颜色错误: {r},{g},{b}"
        finally:
            doc.close()

    def test_no_content_truncated(self):
        """正文内容不被破坏。"""
        out = os.path.join(self.d, "out.pdf")
        PDFHeaderFooter(self.input).apply(
            out, header_text="H", footer_text="F",
            page_number_position="footer-right",
        )
        for i in range(3):
            assert f"body page {i + 1}" in _page_text(out, i)

    def test_invalid_position_rejected(self):
        with pytest.raises(ValueError):
            PDFHeaderFooter(self.input).apply(
                os.path.join(self.d, "x.pdf"),
                header_text="t", header_position="middle",
            )

    def test_invalid_color_rejected(self):
        with pytest.raises(ValueError):
            PDFHeaderFooter(self.input).apply(
                os.path.join(self.d, "x.pdf"),
                header_text="t", header_color="red",
            )

    def test_font_hei_applied(self):
        out = os.path.join(self.d, "out.pdf")
        info = PDFHeaderFooter(self.input).apply(
            out, header_text="黑体页眉测试", header_font="hei",
            page_number_position="none",
        )
        assert info["headers_applied"] == 3
        assert "黑体页眉测试" in _page_text(out)
        # 断言真实字体名（hei 嵌入 Noto Sans SC，song 嵌入 Noto Serif SC）
        doc = fitz.open(out)
        try:
            for b in doc[0].get_text("dict")["blocks"]:
                for l in b.get("lines", []):
                    for s in l["spans"]:
                        if "黑体页眉测试" in s["text"]:
                            assert "Noto Sans" in s["font"], f"黑体选项实际字体: {s['font']}"
        finally:
            doc.close()

    def test_font_song_default_is_song(self):
        out = os.path.join(self.d, "out.pdf")
        PDFHeaderFooter(self.input).apply(
            out, header_text="宋体默认测试",
            page_number_position="none",
        )
        doc = fitz.open(out)
        try:
            for b in doc[0].get_text("dict")["blocks"]:
                for l in b.get("lines", []):
                    for s in l["spans"]:
                        if "宋体默认测试" in s["text"]:
                            assert "Noto Serif" in s["font"], f"宋体默认实际字体: {s['font']}"
        finally:
            doc.close()

    def test_font_western_with_cjk_rejected(self):
        with pytest.raises(ValueError, match="仅支持西文"):
            PDFHeaderFooter(self.input).apply(
                os.path.join(self.d, "x.pdf"),
                header_text="中文内容", header_font="times",
            )

    def test_font_western_with_zh_number_format_rejected(self):
        with pytest.raises(ValueError, match="宋体或黑体"):
            PDFHeaderFooter(self.input).apply(
                os.path.join(self.d, "x.pdf"),
                header_text="English Text", header_font="times",
                page_number_position="header-right",
                page_number_format="zh_total",
                page_font="times",
            )

    def test_font_western_with_english_ok(self):
        out = os.path.join(self.d, "out.pdf")
        info = PDFHeaderFooter(self.input).apply(
            out, header_text="Confidential", header_font="helvetica",
            page_number_position="footer-right",
            page_number_format="plain",
        )
        assert info["headers_applied"] == 3
        assert "Confidential" in _page_text(out)

    def test_invalid_font_rejected(self):
        with pytest.raises(ValueError, match="无效的"):
            PDFHeaderFooter(self.input).apply(
                os.path.join(self.d, "x.pdf"),
                header_text="t", header_font="kaiti",
            )

    def test_independent_styles_per_group(self):
        """页眉黑体11pt红色、页脚宋体8pt蓝色、页码黑体7pt灰色，互不干扰。"""
        out = os.path.join(self.d, "out.pdf")
        PDFHeaderFooter(self.input).apply(
            out,
            header_text="页眉文字",
            header_font="hei", header_font_size=11, header_color="#FF0000",
            footer_text="页脚文字",
            footer_font="song", footer_font_size=8, footer_color="#0000FF",
            page_number_position="footer-right",
            page_font="hei", page_font_size=7, page_color="#888888",
        )
        doc = fitz.open(out)
        try:
            found = {}
            for b in doc[0].get_text("dict")["blocks"]:
                for l in b.get("lines", []):
                    for s in l["spans"]:
                        t = s["text"]
                        if "页眉文字" in t:
                            found["header"] = (s["font"], s["size"], s["color"])
                        elif "页脚文字" in t:
                            found["footer"] = (s["font"], s["size"], s["color"])
            assert found["header"][0].startswith("Noto Sans"), found["header"]
            assert found["header"][1] == 11, found["header"]
            assert found["header"][2] == 0xFF0000, found["header"]  # sRGB int
            assert "Noto Serif" in found["footer"][0], found["footer"]
            assert found["footer"][1] == 8, found["footer"]
            assert found["footer"][2] == 0x0000FF, found["footer"]
        finally:
            doc.close()

    def test_at_least_one_action_required_via_service(self):
        # 服务层：全部为空时 apply 仍执行（路由层负责拦截），验证无操作输出页数不变
        out = os.path.join(self.d, "out.pdf")
        info = PDFHeaderFooter(self.input).apply(
            out, header_text=None, footer_text=None,
            page_number_position="none",
        )
        assert info["page_count"] == 3
