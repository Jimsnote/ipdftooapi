import os
from pypdf import PdfReader
from pdf2docx import Converter

from app.core.logger import get_logger

logger = get_logger(__name__)


def _patch_pdf2docx_zero_area_text() -> None:
    """pdf2docx 0.5.8 隐藏文字过滤未处理零面积文本框，补跳过逻辑。"""
    from pdf2docx.page.RawPageFitz import RawPageFitz

    if getattr(RawPageFitz._preprocess_text, "_ipdftoo_zero_area_patched", False):
        return

    def preprocess_text_safe(self, **settings):
        import logging
        from pdf2docx.common.algorithm import get_area
        from pdf2docx.common.constants import FACTOR_A_HALF

        ocr = settings["ocr"]
        if ocr == 1:
            raise SystemExit("OCR feature is planned but not implemented yet.")

        raw = self.page_engine.get_text("rawdict", flags=64)
        text_blocks = raw.get("blocks", [])
        try:
            spans = self.page_engine.get_texttrace()
        except SystemError:
            logging.warning("Ignore hidden text checking due to UnicodeDecodeError in upstream library.")
            spans = []
        if not spans:
            return text_blocks

        if ocr == 2:
            filtered_spans = [span for span in spans if span["type"] != 3]
        else:
            filtered_spans = [span for span in spans if span["type"] == 3]

        def span_area(bbox):
            x0, y0, x1, y1 = bbox
            return (x1 - x0) * (y1 - y0)

        blocks = []
        for block in text_blocks:
            intersected = False
            for line in block["lines"]:
                for span in line["spans"]:
                    area = span_area(span["bbox"])
                    if area <= 0:
                        continue
                    for filter_span in filtered_spans:
                        intersected_area = get_area(span["bbox"], filter_span["bbox"])
                        if (
                            intersected_area / area >= FACTOR_A_HALF
                            and span["font"] == filter_span["font"]
                        ):
                            intersected = True
                            break
                    if intersected:
                        break
                if intersected:
                    break
            if not intersected:
                blocks.append(block)
        return blocks

    preprocess_text_safe._ipdftoo_zero_area_patched = True
    RawPageFitz._preprocess_text = preprocess_text_safe


_patch_pdf2docx_zero_area_text()


class PDFToWordConverter:
    """Convert PDF files to Word (.docx) format using pdf2docx."""

    def convert(self, input_path: str, output_path: str, pages: str = None) -> dict:
        """Convert a PDF file to Word document.

        Args:
            input_path: Path to the input PDF file.
            output_path: Path for the output .docx file.
            pages: Page range string, 1-based, e.g. "1,3,5" or "1-4"（与全站
                split/to-jpg/to-markdown 等端点的 1 基语义一致）。
                None means all pages.

        Returns:
            dict with page_count info.
        """
        logger.info(f"Starting PDF to Word conversion: {input_path} -> {output_path}")

        cv = Converter(input_path)
        try:
            # pdf2docx pages param: list of page numbers (0-based)
            page_list = None
            if pages:
                try:
                    total = len(PdfReader(input_path).pages)
                except Exception as e:
                    msg = str(e).lower()
                    if "decrypt" in msg or "password" in msg or "encrypt" in msg:
                        raise ValueError("PDF 已加密，请先用「解除 PDF 密码」工具解密后再转换")
                    raise
                page_list = self._parse_pages(pages, total)

            cv.convert(output_path, start=0, end=None, pages=page_list)

            page_count = len(cv.pages) if hasattr(cv, "pages") else 0
            logger.info(f"PDF to Word conversion completed: {page_count} pages")
            return {"page_count": page_count}
        finally:
            cv.close()

    @staticmethod
    def _parse_pages(pages: str, total_pages: int) -> list:
        """Parse page string like '1,3,5' or '1-4' (1-based) into a clamped,
        deduplicated 0-based list for pdf2docx.

        审计 #14：对外语义改为 1 基（此前是 0 基，与全站 split/to-jpg/to-markdown
        相反，pages="1" 会静默转第 2 页）。先夹到 [1, total_pages] 再展开防 DoS，
        最终换算回 pdf2docx 需要的 0 基列表。
        """
        result = set()
        for part in pages.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                start, end = part.split("-", 1)
                start_num = int(start)
                end_num = int(end)
                if start_num > end_num:
                    raise ValueError(f"页码区间无效：{part}")
                # 夹取后再迭代，防 "1-99999999" 这类超大区间
                result.update(range(max(1, start_num), min(end_num, total_pages) + 1))
            else:
                p = int(part)
                if 1 <= p <= total_pages:
                    result.add(p)
        # 换算为 pdf2docx 的 0 基页号
        result = sorted(p - 1 for p in result)
        if not result:
            raise ValueError("没有有效的页码范围")
        return result
