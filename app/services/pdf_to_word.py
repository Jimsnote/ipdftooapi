import os
from pypdf import PdfReader
from pdf2docx import Converter

from app.core.logger import get_logger

logger = get_logger(__name__)


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
