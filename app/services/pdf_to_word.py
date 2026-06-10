import os
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
            pages: Page range string, e.g. "0,2,5" or "0-3". None means all pages.

        Returns:
            dict with page_count info.
        """
        logger.info(f"Starting PDF to Word conversion: {input_path} -> {output_path}")

        cv = Converter(input_path)
        try:
            # pdf2docx pages param: list of page numbers (0-based)
            page_list = None
            if pages:
                page_list = self._parse_pages(pages)

            cv.convert(output_path, start=0, end=None, pages=page_list)

            page_count = len(cv.pages) if hasattr(cv, "pages") else 0
            logger.info(f"PDF to Word conversion completed: {page_count} pages")
            return {"page_count": page_count}
        finally:
            cv.close()

    @staticmethod
    def _parse_pages(pages: str) -> list:
        """Parse page string like '0,2,5' or '0-3' into list of int."""
        result = []
        for part in pages.split(","):
            part = part.strip()
            if "-" in part:
                start, end = part.split("-", 1)
                result.extend(range(int(start), int(end) + 1))
            else:
                result.append(int(part))
        return result
