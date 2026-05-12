import os
from typing import List
from pypdf import PdfWriter

from app.core.logger import get_logger

logger = get_logger(__name__)


class PDFMerger:
    def __init__(self, file_paths: List[str]):
        self.file_paths = file_paths

    def merge(self, output_path: str) -> str:
        if not self.file_paths:
            raise ValueError("No PDF files to merge")

        writer = PdfWriter()
        for path in self.file_paths:
            if not os.path.exists(path):
                raise FileNotFoundError(f"File not found: {path}")
            writer.append(path)

        with open(output_path, "wb") as f:
            writer.write(f)
        writer.close()

        logger.info(f"Merged {len(self.file_paths)} PDFs into {output_path}")
        return output_path
