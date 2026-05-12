import os
from typing import List
from pypdf import PdfReader, PdfWriter

from app.core.logger import get_logger

logger = get_logger(__name__)


class PDFSplitter:
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.reader = PdfReader(file_path)
        self.total_pages = len(self.reader.pages)

    def split(
        self, mode: str = "all", value: str = "", output_dir: str = "."
    ) -> List[str]:
        if self.total_pages == 0:
            raise ValueError("PDF has no pages")

        if mode == "all":
            return self._split_all(output_dir)
        elif mode == "ranges":
            return self._split_ranges(value, output_dir)
        elif mode == "fixed":
            return self._split_fixed(int(value) if value else 1, output_dir)
        else:
            raise ValueError(f"Unknown split mode: {mode}")

    def _split_all(self, output_dir: str) -> List[str]:
        """Extract each page as a separate PDF."""
        output_paths = []
        for i, page in enumerate(self.reader.pages):
            writer = PdfWriter()
            writer.add_page(page)
            path = os.path.join(output_dir, f"page_{i + 1}.pdf")
            with open(path, "wb") as f:
                writer.write(f)
            output_paths.append(path)
        logger.info(f"Split into {len(output_paths)} single-page PDFs")
        return output_paths

    def _split_ranges(self, ranges_str: str, output_dir: str) -> List[str]:
        """Split by page ranges like '1-3,5-10'."""
        output_paths = []
        parts = [p.strip() for p in ranges_str.split(",")]

        for idx, part in enumerate(parts):
            if "-" in part:
                start, end = part.split("-")
                start_page = max(0, int(start) - 1)
                end_page = min(self.total_pages, int(end))
            else:
                start_page = int(part) - 1
                end_page = start_page + 1

            writer = PdfWriter()
            for i in range(start_page, end_page):
                if 0 <= i < self.total_pages:
                    writer.add_page(self.reader.pages[i])

            path = os.path.join(output_dir, f"part_{idx + 1}.pdf")
            with open(path, "wb") as f:
                writer.write(f)
            output_paths.append(path)

        logger.info(f"Split into {len(output_paths)} range-based PDFs")
        return output_paths

    def _split_fixed(self, pages_per_file: int, output_dir: str) -> List[str]:
        """Split into files with fixed number of pages."""
        output_paths = []
        current_writer = PdfWriter()
        file_count = 0

        for i, page in enumerate(self.reader.pages):
            current_writer.add_page(page)
            if (i + 1) % pages_per_file == 0 or i == self.total_pages - 1:
                file_count += 1
                path = os.path.join(output_dir, f"part_{file_count}.pdf")
                with open(path, "wb") as f:
                    current_writer.write(f)
                output_paths.append(path)
                current_writer = PdfWriter()

        logger.info(f"Split into {len(output_paths)} fixed-size PDFs")
        return output_paths
