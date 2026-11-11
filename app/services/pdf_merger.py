import os
from typing import List
from pypdf import PdfReader, PdfWriter

from app.core.logger import get_logger

logger = get_logger(__name__)


def _read_without_invalid_link_actions(path: str) -> PdfReader:
    reader = PdfReader(path)
    for page_number, page in enumerate(reader.pages, start=1):
        for ref in page.get("/Annots", []):
            annotation = ref.get_object()
            action = annotation.get("/A")
            if action is not None and "/S" not in action.get_object():
                logger.warning(
                    "忽略无效链接动作: %s 第 %d 页",
                    os.path.basename(path),
                    page_number,
                )
                del annotation["/A"]
    return reader


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
            writer.append(_read_without_invalid_link_actions(path))

        with open(output_path, "wb") as f:
            writer.write(f)
        writer.close()

        logger.info(f"Merged {len(self.file_paths)} PDFs into {output_path}")
        return output_path
