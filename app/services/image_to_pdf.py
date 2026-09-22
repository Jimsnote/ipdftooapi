import os
from typing import List, Literal
from PIL import Image
from app.core.logger import get_logger

logger = get_logger(__name__)

# A4 dimensions in pixels at 150 DPI
A4_PORTRAIT = (1240, 1754)   # 210mm x 297mm @ 150 DPI
A4_LANDSCAPE = (1754, 1240)  # 297mm x 210mm @ 150 DPI


class ImageToPDFConverter:
    """Convert image files (JPG/PNG) to PDF using Pillow."""

    @staticmethod
    def convert(
        image_paths: List[str],
        output_path: str,
        orientation: Literal["portrait", "landscape"] = "portrait",
        fit_mode: Literal["fit", "fill", "original"] = "fit",
    ) -> str:
        """
        Convert one or more images to a single PDF file.

        Args:
            image_paths: List of image file paths.
            output_path: Output PDF file path.
            orientation: Page orientation - 'portrait' or 'landscape'.
            fit_mode: How images are placed on the page:
                - 'fit': Scale to fit within page margins (default, preserves aspect ratio)
                - 'fill': Scale to fill the entire page (may crop edges)
                - 'original': Use original image size, centered on page

        Returns:
            Path to the generated PDF file.
        """
        if not image_paths:
            raise ValueError("至少需要一张图片")

        page_size = A4_PORTRAIT if orientation == "portrait" else A4_LANDSCAPE
        margin = 36  # ~6mm margin in pixels @ 150 DPI
        canvas_w = page_size[0] - margin * 2
        canvas_h = page_size[1] - margin * 2

        pil_images = []
        for path in image_paths:
            img = Image.open(path)
            # Convert to RGB if necessary (handles RGBA, P mode, etc.)
            if img.mode in ("RGBA", "P", "LA", "L"):
                # For transparent images, composite on white background
                bg = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                if img.mode in ("RGBA", "LA"):
                    bg.paste(img, mask=img.split()[-1])
                    img = bg
                else:
                    img = img.convert("RGB")
            elif img.mode != "RGB":
                img = img.convert("RGB")

            # Resize according to fit_mode
            if fit_mode == "original":
                # Center the image on the page
                paste_x = margin + max(0, (canvas_w - img.width) // 2)
                paste_y = margin + max(0, (canvas_h - img.height) // 2)
                canvas = Image.new("RGB", page_size, (255, 255, 255))
                canvas.paste(img, (paste_x, paste_y))
                img = canvas
            elif fit_mode == "fill":
                # Scale to fill entire page (may crop)
                img_ratio = img.width / img.height
                page_ratio = page_size[0] / page_size[1]
                if img_ratio > page_ratio:
                    # Image is wider, scale to match height then crop width
                    new_h = page_size[1]
                    new_w = int(new_h * img_ratio)
                    img = img.resize((new_w, new_h), Image.LANCZOS)
                    left = (new_w - page_size[0]) // 2
                    img = img.crop((left, 0, left + page_size[0], page_size[1]))
                else:
                    # Image is taller, scale to match width then crop height
                    new_w = page_size[0]
                    new_h = int(new_w / img_ratio)
                    img = img.resize((new_w, new_h), Image.LANCZOS)
                    top = (new_h - page_size[1]) // 2
                    img = img.crop((0, top, page_size[0], top + page_size[1]))
            else:
                # fit mode - scale to fit within margins
                img_ratio = img.width / img.height
                canvas_ratio = canvas_w / canvas_h
                if img_ratio > canvas_ratio:
                    new_w = canvas_w
                    new_h = int(new_w / img_ratio)
                else:
                    new_h = canvas_h
                    new_w = int(new_h * img_ratio)

                # 极端长宽比时防 0 尺寸（PIL resize 对 0 维度报错）
                new_w = max(1, new_w)
                new_h = max(1, new_h)
                img = img.resize((new_w, new_h), Image.LANCZOS)
                paste_x = margin + (canvas_w - new_w) // 2
                paste_y = margin + (canvas_h - new_h) // 2
                canvas = Image.new("RGB", page_size, (255, 255, 255))
                canvas.paste(img, (paste_x, paste_y))
                img = canvas

            pil_images.append(img)

        # Save as PDF
        first_image = pil_images[0]
        rest_images = pil_images[1:] if len(pil_images) > 1 else []

        first_image.save(
            output_path,
            "PDF",
            resolution=150.0,
            save_all=True,
            append_images=rest_images,
        )

        logger.info(f"Image to PDF conversion completed: {len(pil_images)} image(s) -> {output_path}")
        return output_path
