import os
import zipfile
from typing import List, Literal
import fitz  # PyMuPDF
from PIL import Image
from app.core.logger import get_logger

logger = get_logger(__name__)


class PDFToImageConverter:
    """Convert PDF pages to image files (JPG/PNG) using PyMuPDF."""

    def __init__(self, input_path: str):
        self.input_path = input_path
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"Input file not found: {input_path}")

    def convert(
        self,
        output_dir: str,
        format: Literal["jpg", "png"] = "jpg",
        dpi: int = 150,
        pages: str = "all",
    ) -> List[str]:
        """
        Convert PDF pages to images.

        Args:
            output_dir: Directory to save output images.
            format: Output image format - 'jpg' or 'png'.
            dpi: Resolution for rendering (72~300).
            pages: Page selection - 'all' or comma-separated like '1,3,5'.

        Returns:
            List of output image file paths.
        """
        os.makedirs(output_dir, exist_ok=True)
        doc = fitz.open(self.input_path)
        total_pages = len(doc)

        # Determine which pages to render
        if pages == "all":
            page_indices = list(range(total_pages))
        else:
            try:
                page_indices = []
                for part in pages.split(","):
                    part = part.strip()
                    if "-" in part:
                        start, end = part.split("-")
                        page_indices.extend(range(int(start) - 1, int(end)))
                    else:
                        page_indices.append(int(part) - 1)
                # Validate and deduplicate
                page_indices = sorted(set(p for p in page_indices if 0 <= p < total_pages))
            except ValueError:
                doc.close()
                raise ValueError("页码格式无效，请使用逗号或连字符分隔，如：1,3,5 或 1-5")

        if not page_indices:
            doc.close()
            raise ValueError("没有有效的页面可供转换")

        zoom = dpi / 72.0  # PyMuPDF default is 72 DPI
        mat = fitz.Matrix(zoom, zoom)

        output_paths: List[str] = []
        ext = "jpg" if format == "jpg" else "png"

        for idx in page_indices:
            page = doc.load_page(idx)
            pix = page.get_pixmap(matrix=mat)

            # Convert to PIL for JPEG quality control
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

            output_name = f"page_{idx + 1:03d}.{ext}"
            output_path = os.path.join(output_dir, output_name)

            if format == "jpg":
                img.save(output_path, "JPEG", quality=90, optimize=True)
            else:
                img.save(output_path, "PNG", optimize=True)

            output_paths.append(output_path)
            logger.debug(f"Rendered page {idx + 1} -> {output_path}")

        doc.close()
        logger.info(f"PDF to image conversion completed: {len(output_paths)} page(s)")
        return output_paths

    @staticmethod
    def zip_images(image_paths: List[str], zip_path: str) -> str:
        """Pack multiple images into a ZIP archive."""
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in image_paths:
                zf.write(path, os.path.basename(path))
        logger.info(f"Created ZIP archive: {zip_path} ({len(image_paths)} files)")
        return zip_path
