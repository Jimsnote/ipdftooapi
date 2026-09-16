import os
from typing import List, Set
from pypdf import PdfReader, PdfWriter
from app.core.logger import get_logger

logger = get_logger(__name__)


class PDFPageRemover:
    """Remove specified pages from a PDF file."""

    def __init__(self, input_path: str):
        self.input_path = input_path
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"Input file not found: {input_path}")

    @property
    def total_pages(self) -> int:
        """Return total number of pages in the PDF."""
        reader = PdfReader(self.input_path)
        return len(reader.pages)

    def remove_pages(self, pages_to_remove: Set[int], output_path: str) -> int:
        """
        Remove specified pages (1-based) and save the result.

        Args:
            pages_to_remove: Set of 1-based page numbers to remove.
            output_path: Path to save the resulting PDF.

        Returns:
            Number of pages in the output PDF.
        """
        reader = PdfReader(self.input_path)
        writer = PdfWriter()

        total = len(reader.pages)
        removed = set()

        for i in range(total):
            page_num = i + 1  # Convert to 1-based
            if page_num not in pages_to_remove:
                writer.add_page(reader.pages[i])
            else:
                removed.add(page_num)

        if len(writer.pages) == 0:
            raise ValueError("不能删除所有页面，PDF 至少需要保留一页")

        with open(output_path, "wb") as f:
            writer.write(f)

        logger.info(f"Removed {len(removed)} page(s) from PDF: {self.input_path} -> {output_path}")
        return len(writer.pages)

    @staticmethod
    def parse_page_list(pages_str: str, total_pages: int) -> Set[int]:
        """
        Parse a page list string into a set of 1-based page numbers.

        Supports formats like:
        - "1,3,5" -> {1, 3, 5}
        - "2-4" -> {2, 3, 4}
        - "1,3-5,7" -> {1, 3, 4, 5, 7}
        """
        result = set()
        parts = [p.strip() for p in pages_str.split(",") if p.strip()]

        for part in parts:
            if "-" in part:
                start, end = part.split("-", 1)
                start_num = int(start.strip())
                end_num = int(end.strip())
                if start_num > end_num:
                    raise ValueError(f"页码区间无效：{part}")
                # 区间先夹到 [1, total_pages] 再迭代：防超大区间 DoS
                for p in range(max(1, start_num), min(end_num, total_pages) + 1):
                    result.add(p)
            else:
                p = int(part.strip())
                if 1 <= p <= total_pages:
                    result.add(p)

        return result
