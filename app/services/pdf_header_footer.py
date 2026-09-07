import os
from datetime import date
from typing import Optional

import fitz  # PyMuPDF

from app.core.logger import get_logger

logger = get_logger(__name__)

# 内嵌开源中文字体（Noto SC，OFL 协议，Google 开源可分发）
# PyMuPDF 内置 CJK（china-s/china-ss）实为同一字体 Droid Sans Fallback（无衬线/无汉字笔画粗细对比），
# 与 Word 真黑体（SimHei）/真宋体（SimSun）观感差异大；嵌入 Noto SC 子集化后每份 PDF 只增加几 KB。
_FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "assets", "fonts")
_SONG_FONTFILE = os.path.join(_FONT_DIR, "NotoSerifSC-Regular.ttf")  # 宋体（衬线）
_HEI_FONTFILE = os.path.join(_FONT_DIR, "NotoSansSC-Regular.ttf")   # 黑体（无衬线）


class PDFHeaderFooter:
    """Add header text and page numbers to an existing PDF.

    Design goals (ruanzhu / bid / contract scenarios):
    - Header text with variables: {filename}, {date}, {year}, {page}, {pages}
    - Page number with 6 positions and multiple formats (第X页 / X / 第X页 共Y页 / X / Y)
    - CJK-safe fonts: 嵌入 Noto Serif SC（宋体）+ Noto Sans SC（黑体）+ Base14 西文
      （PyMuPDF 内置 china-s 实为 Droid Sans Fallback，与 Word 真黑体差异大，
      故宋/黑均嵌入 OFL 开源 Noto 字体；subset_fonts() 后每份仅增几 KB）
    - Never obscure existing content: text is drawn inside the page margin zone;
      when margin is too small the text may overlap content, so we keep default
      margin 36pt (1.27cm) which matches typical Word header area.
    """

    # 位置：页眉左/中/右、页脚左/中/右
    POSITIONS = ("header-left", "header-center", "header-right",
                 "footer-left", "footer-center", "footer-right")

    # 字体映射：display name -> dict(builtin=fitz内置名 或 file=字体文件路径, cjk=是否支持中文)
    # 嵌入文件型字体 vs PyMuPDF 内置别名走不同通道（_resolve_fitz_font 区分）
    # 同页混用宋体+黑体时各自有独立 fontname，不会互相覆盖
    FONTS = {
        "song": {"file": _SONG_FONTFILE, "cjk": True, "alias": "Song0nt"},  # 宋体（衬线，嵌入 Noto Serif SC）
        "hei": {"file": _HEI_FONTFILE, "cjk": True, "alias": "Hei0nt"},     # 黑体（无衬线，嵌入 Noto Sans SC）
        "times": {"builtin": "tiro", "cjk": False},                          # Times-Roman（仅西文）
        "helvetica": {"builtin": "helv", "cjk": False},                       # Helvetica（仅西文）
    }
    DEFAULT_FONT = "song"

    # 类级缓存：fitz.Font 对象（宽度计算用）
    _font_obj_cache: dict = {}
    # 嵌入字体文件可用性（key: font_key, value: bool）
    _fontfile_available: dict = {}

    def __init__(self, input_path: str):
        self.input_path = input_path
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"Input file not found: {input_path}")

    @classmethod
    def _get_font_obj(cls, font_key: str):
        """获取 fitz.Font 对象（用于文本宽度计算），带类级缓存。"""
        if font_key in cls._font_obj_cache:
            return cls._font_obj_cache[font_key]
        spec = cls.FONTS[font_key]
        if "file" in spec:
            font = fitz.Font(fontfile=spec["file"])
        else:
            font = fitz.Font(spec["builtin"])
        cls._font_obj_cache[font_key] = font
        return font

    @classmethod
    def _resolve_fitz_font(cls, font_key: str):
        """返回 (fontname, fontfile) 供 page.insert_text 使用。

        - 内置字体：返回 (builtin名, None)
        - 文件字体：返回 (spec.alias, 文件路径) —— 同一 PDF 内每个文件型字体用独立 alias，
          避免同页混用宋+黑时后者覆盖前者。
        """
        spec = cls.FONTS[font_key]
        if "builtin" in spec:
            return spec["builtin"], None
        return spec["alias"], spec["file"]

    def apply(
        self,
        output_path: str,
        header_text: Optional[str] = None,
        header_position: str = "header-center",
        footer_text: Optional[str] = None,
        footer_position: str = "footer-center",
        page_number_position: str = "header-right",
        page_number_format: str = "plain",
        page_number_start: int = 1,
        margin: float = 36.0,
        apply_from_page: int = 1,
        # 三组独立样式：页眉 / 页脚 / 页码（字体、字号、颜色）
        header_font: str = "song",
        header_font_size: float = 9.0,
        header_color: str = "#000000",
        footer_font: str = "song",
        footer_font_size: float = 9.0,
        footer_color: str = "#000000",
        page_font: str = "song",
        page_font_size: float = 9.0,
        page_color: str = "#000000",
    ) -> dict:
        """Apply header/footer/page numbers and save.

        Header, footer and page number each carry independent
        font/size/color so users can differentiate them from the
        original document's typography.

        Returns dict with page_count and applied counts.
        """
        style_groups = [
            ("页眉", header_text, header_font, header_font_size, header_color, True),
            ("页脚", footer_text, footer_font, footer_font_size, footer_color, True),
        ]
        # 页码组：格式化文本由 format 决定，需单独校验中文页码格式
        style_groups.append(
            ("页码", None, page_font, page_font_size, page_color, False)
        )

        parsed_styles = {}
        uses_embedded_font = False
        for name, text, font, size, color, check_text in style_groups:
            if font not in self.FONTS:
                raise ValueError(f"无效的{name}字体: {font}")
            spec = self.FONTS[font]
            supports_cjk = spec["cjk"]
            if "file" in spec:
                if font not in self._fontfile_available:
                    self._fontfile_available[font] = os.path.exists(spec["file"])
                if not self._fontfile_available[font]:
                    raise ValueError(
                        f"{name}字体文件缺失: {os.path.basename(spec['file'])}"
                    )
                uses_embedded_font = True
            if check_text and text and not supports_cjk and self._has_cjk(text):
                raise ValueError(f"{name}含中文内容，当前字体仅支持西文，请选择宋体或黑体")
            if not check_text:
                # 页码：格式含中文（第X页）时西文字体不可用
                if (
                    not supports_cjk
                    and page_number_position != "none"
                    and page_number_format in ("zh", "zh_total")
                ):
                    raise ValueError("中文页码格式需选择宋体或黑体的页码字体")
            if not (4 <= size <= 24):
                raise ValueError(f"{name}字号需在 4-24 之间")
            rgb = self._parse_color(color)
            parsed_styles[name] = (font, size, (rgb["r"], rgb["g"], rgb["b"]))

        if header_position not in self.POSITIONS:
            raise ValueError(f"无效的页眉位置: {header_position}")
        if footer_position not in self.POSITIONS:
            raise ValueError(f"无效的页脚位置: {footer_position}")
        if page_number_position not in self.POSITIONS and page_number_position != "none":
            raise ValueError(f"无效的页码位置: {page_number_position}")
        if not (1 <= page_number_start <= 100000):
            raise ValueError("页码起始值无效")
        if not (18 <= margin <= 90):
            raise ValueError("边距需在 18-90 磅之间")
        if header_text and len(header_text) > 200:
            raise ValueError("页眉文字过长（上限 200 字符）")
        if footer_text and len(footer_text) > 200:
            raise ValueError("页脚文字过长（上限 200 字符）")

        doc = fitz.open(self.input_path)
        total = len(doc)
        if total == 0:
            doc.close()
            raise ValueError("PDF 没有任何页面")
        if not (1 <= apply_from_page <= total):
            doc.close()
            raise ValueError(f"起始页需在 1-{total} 之间")

        base_name = self._base_filename()

        h_font, h_size, h_color = parsed_styles["页眉"]
        f_font, f_size, f_color = parsed_styles["页脚"]
        p_font, p_size, p_color = parsed_styles["页码"]
        headers_applied = 0
        footers_applied = 0
        numbers_applied = 0

        try:
            for i, page in enumerate(doc):
                page_no_1based = i + 1
                if page_no_1based < apply_from_page:
                    continue

                w, h = page.rect.width, page.rect.height

                # --- 页眉 ---
                if header_text:
                    text = self._render_vars(
                        header_text, base_name,
                        page_number_start + (page_no_1based - apply_from_page),
                        total,
                    )
                    self._draw_text(page, text, header_position, w, h,
                                    h_size, margin, h_color, h_font)
                    headers_applied += 1

                # --- 页脚 ---
                if footer_text:
                    text = self._render_vars(
                        footer_text, base_name,
                        page_number_start + (page_no_1based - apply_from_page),
                        total,
                    )
                    self._draw_text(page, text, footer_position, w, h,
                                    f_size, margin, f_color, f_font)
                    footers_applied += 1

                # --- 页码 ---
                if page_number_position != "none":
                    num = page_number_start + (page_no_1based - apply_from_page)
                    text = self._format_page_number(page_number_format, num, total)
                    self._draw_text(page, text, page_number_position, w, h,
                                    p_size, margin, p_color, p_font)
                    numbers_applied += 1

            # 嵌入字体（宋体）子集化：只保留实际用到的字形，输出仅增几 KB
            if uses_embedded_font:
                doc.subset_fonts()
            doc.save(output_path, garbage=3, deflate=True)
        finally:
            doc.close()

        logger.info(
            f"Header/footer applied to {self.input_path} -> {output_path}: "
            f"{headers_applied} headers, {footers_applied} footers, {numbers_applied} page numbers"
        )
        return {
            "page_count": total,
            "headers_applied": headers_applied,
            "footers_applied": footers_applied,
            "numbers_applied": numbers_applied,
        }

    # ------------------------------------------------------------------

    def _draw_text(self, page, text, position, w, h, font_size, margin, stroke_color,
                   font_key="hei"):
        """Draw text at the given logical position (baseline inside margin)."""
        if position.startswith("header"):
            y = margin - font_size * 0.3  # 基线略高于边距线，视觉居中于页眉带
        else:
            y = h - margin + font_size * 1.0

        # 宽度计算：统一走 fitz.Font（内置别名与文件字体都支持）
        font_obj = self._get_font_obj(font_key)
        tw = font_obj.text_length(text, fontsize=font_size)

        if position.endswith("left"):
            x = margin
        elif position.endswith("center"):
            x = (w - tw) / 2
        else:  # right
            x = w - margin - tw

        # 边界保护：极窄页面（如裁切过的 PDF）防止负坐标
        x = max(0, x)
        y = min(max(y, font_size), h - 2)

        fontname, fontfile = self._resolve_fitz_font(font_key)
        if fontfile:
            page.insert_text(
                fitz.Point(x, y),
                text,
                fontname=fontname,
                fontfile=fontfile,  # 嵌入字体文件（宋体），subset_fonts() 前全量嵌入
                fontsize=font_size,
                color=stroke_color,
                overlay=True,
            )
        else:
            page.insert_text(
                fitz.Point(x, y),
                text,
                fontname=fontname,  # pymupdf 内置字体，零依赖
                fontsize=font_size,
                color=stroke_color,
                overlay=True,
            )

    @staticmethod
    def _has_cjk(text: str) -> bool:
        """检测文本是否含 CJK 字符（含中文标点）。"""
        for ch in text:
            if "\u4e00" <= ch <= "\u9fff" or "\u3000" <= ch <= "\u303f" or "\uff00" <= ch <= "\uffef":
                return True
        return False

    def _render_vars(self, text: str, base_name: str, page_num: int, total: int) -> str:
        today = date.today()
        replacements = {
            "{filename}": base_name,
            "{date}": today.strftime("%Y-%m-%d"),
            "{year}": str(today.year),
            "{page}": str(page_num),
            "{pages}": str(total),
        }
        for k, v in replacements.items():
            text = text.replace(k, v)
        return text

    def _format_page_number(self, fmt: str, num: int, total: int) -> str:
        if fmt == "plain":
            return str(num)
        if fmt == "dashed":
            return f"{num} / {total}"
        if fmt == "zh":
            return f"第 {num} 页"
        if fmt == "zh_total":
            return f"第 {num} 页 共 {total} 页"
        raise ValueError(f"无效的页码格式: {fmt}")

    def _base_filename(self) -> str:
        name = os.path.basename(self.input_path)
        stem = os.path.splitext(name)[0]
        return stem if stem else "document"

    @staticmethod
    def _parse_color(color: str) -> dict:
        """Parse '#RRGGBB' or 'RRGGBB' into r/g/b floats in [0,1]."""
        c = color.strip().lstrip("#")
        if len(c) != 6:
            raise ValueError("颜色格式无效，应为 #RRGGBB")
        try:
            r = int(c[0:2], 16) / 255.0
            g = int(c[2:4], 16) / 255.0
            b = int(c[4:6], 16) / 255.0
        except ValueError:
            raise ValueError("颜色格式无效，应为 #RRGGBB")
        return {"r": r, "g": g, "b": b}
