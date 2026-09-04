"""Invoice merger service: analyze PDF dimensions and render onto A4 layout."""

import os
import math
from dataclasses import dataclass
from typing import List, Dict, Any

import fitz  # PyMuPDF
from PIL import Image, ImageDraw, ImageFont

from app.core.logger import get_logger

logger = get_logger(__name__)

A4_PX_300 = (2480, 3508)          # 竖版 A4 @ 300 DPI
A4_LANDSCAPE_PX_300 = (3508, 2480)  # 横版 A4 @ 300 DPI（paste_sheet 用）

# 凭证粘贴模式（paste_sheet）：右侧网格 = {per_page: (cols, rows, rotate_deg)}
# - 2 张：1 列 2 行 portrait 槽，发票旋转 90° 竖贴（约 80% 原大）
# - 4 张：2 列 2 行 landscape 槽，发票正放（约 52% 原大）
PASTE_GRID = {
    2: (1, 2, 90),
    4: (2, 2, 0),
}
PASTE_DEFAULT_BINDING_MM = 30.0  # 左侧装订线默认宽度（财务凭证惯例）


@dataclass
class InvoiceInfo:
    filename: str
    path: str
    original_width: float
    original_height: float
    page_count: int


class InvoiceMerger:
    def __init__(self):
        self._infos: List[InvoiceInfo] = []

    def analyze(self, paths: List[str]) -> List[InvoiceInfo]:
        """Analyze all PDF files and return page dimension info."""
        results: List[InvoiceInfo] = []
        for p in paths:
            try:
                doc = fitz.open(p)
                page = doc[0]
                rect = page.rect
                results.append(
                    InvoiceInfo(
                        filename=os.path.basename(p),
                        path=p,
                        original_width=rect.width,
                        original_height=rect.height,
                        page_count=len(doc),
                    )
                )
                doc.close()
            except Exception as e:
                logger.error(f"Failed to analyze {p}: {e}")
                raise
        self._infos = results
        return results

    def merge(
        self,
        output_path: str,
        per_page: int = 4,
        margin: str = "standard",
        crop_marks: bool = True,
        page_numbers: bool = True,
        binding_mm: float = 0,
        layout: str = "stacked",
    ) -> Dict[str, Any]:
        """Render analyzed invoices into A4 PDF with grid layout.

        layout:
        - "stacked"（默认）：竖版 A4，per_page 任意档，行为与历史版本完全一致；
        - "paste_sheet"：横版 A4 + 左侧装订线 gutter（binding_mm，默认 30mm），
          per_page 仅支持 2（1×2 portrait 槽，发票旋转 90°）或 4（2×2 槽，正放）。
          统一化方案 §11 / 用户确认的凭证粘贴样式。
        """
        if not self._infos:
            raise RuntimeError("Must call analyze() before merge()")

        if layout == "paste_sheet":
            if per_page not in PASTE_GRID:
                raise ValueError("凭证粘贴模式仅支持每页 2 张或 4 张")
            if binding_mm <= 0:
                binding_mm = PASTE_DEFAULT_BINDING_MM
        elif layout != "stacked":
            raise ValueError(f"未知布局模式: {layout}")

        # margin mapping to mm
        margin_mm = {"narrow": 5.0, "standard": 10.0, "wide": 15.0}.get(margin, 10.0)

        # Grid config
        grid_map = {
            1: (1, 1),
            2: (1, 2),
            4: (2, 2),
            6: (2, 3),
            9: (3, 3),
        }
        paste = layout == "paste_sheet"
        if paste:
            cols, rows, rotate_deg = PASTE_GRID[per_page]
            w_px, h_px = A4_LANDSCAPE_PX_300
        else:
            cols, rows = grid_map.get(per_page, (2, 2))
            rotate_deg = 0
            w_px, h_px = A4_PX_300

        margin_px = int(margin_mm / 25.4 * 300)
        binding_px = int(binding_mm / 25.4 * 300)
        if paste:
            # 横版 + 左侧装订线死区：右侧内容区 = 页宽 - gutter - 右边距
            content_x = binding_px
            inner_w = w_px - binding_px - margin_px
            inner_h = h_px - margin_px * 2
        else:
            content_x = margin_px
            inner_w = w_px - margin_px * 2 - binding_px
            inner_h = h_px - margin_px * 2
        gap_px = max(4, int(3 / 25.4 * 300))  # ~3mm gap

        slot_w = (inner_w - gap_px * (cols - 1)) / cols
        slot_h = (inner_h - gap_px * (rows - 1)) / rows

        all_pages: List[fitz.Page] = []
        docs: List[fitz.Document] = []
        for info in self._infos:
            doc = fitz.open(info.path)
            docs.append(doc)
            for page in doc:
                all_pages.append(page)

        total_pages = math.ceil(len(all_pages) / per_page)
        rendered_images: List[Image.Image] = []

        for page_idx in range(total_pages):
            canvas = Image.new("RGB", (w_px, h_px), "white")
            draw = ImageDraw.Draw(canvas)

            chunk = all_pages[page_idx * per_page : (page_idx + 1) * per_page]
            for slot_idx, page in enumerate(chunk):
                col = slot_idx % cols
                row = slot_idx // cols
                if paste:
                    x = content_x + col * (slot_w + gap_px)
                else:
                    x = content_x + col * (slot_w + gap_px) + (binding_px if binding_mm > 0 else 0)
                y = margin_px + row * (slot_h + gap_px)

                # Render page to image at 300 DPI
                mat = fitz.Matrix(300 / 72, 300 / 72)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)

                # paste_sheet 的 2 张档：发票旋转 90° 竖贴（内容正向可读）
                if rotate_deg:
                    img = img.rotate(rotate_deg, expand=True)

                # Fit to slot preserving aspect
                iw, ih = img.size
                ratio = iw / ih
                slot_ratio = slot_w / slot_h
                if ratio > slot_ratio:
                    new_w = int(slot_w)
                    new_h = int(new_w / ratio)
                else:
                    new_h = int(slot_h)
                    new_w = int(new_h * ratio)
                if new_w > 0 and new_h > 0:
                    try:
                        img = img.resize((new_w, new_h), Image.LANCZOS)
                    except AttributeError:
                        img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

                paste_x = int(x + (slot_w - new_w) / 2)
                paste_y = int(y + (slot_h - new_h) / 2)
                canvas.paste(img, (paste_x, paste_y))

                # Crop marks
                if crop_marks:
                    mark = max(8, int(3 / 25.4 * 300))
                    # Top-left
                    draw.line([(paste_x, paste_y - mark), (paste_x, paste_y)], fill="#aaaaaa", width=1)
                    draw.line([(paste_x - mark, paste_y), (paste_x, paste_y)], fill="#aaaaaa", width=1)
                    # Top-right
                    draw.line([(paste_x + new_w, paste_y - mark), (paste_x + new_w, paste_y)], fill="#aaaaaa", width=1)
                    draw.line([(paste_x + new_w, paste_y), (paste_x + new_w + mark, paste_y)], fill="#aaaaaa", width=1)
                    # Bottom-left
                    draw.line([(paste_x, paste_y + new_h), (paste_x, paste_y + new_h + mark)], fill="#aaaaaa", width=1)
                    draw.line([(paste_x - mark, paste_y + new_h), (paste_x, paste_y + new_h)], fill="#aaaaaa", width=1)
                    # Bottom-right
                    draw.line([(paste_x + new_w, paste_y + new_h), (paste_x + new_w, paste_y + new_h + mark)], fill="#aaaaaa", width=1)
                    draw.line([(paste_x + new_w, paste_y + new_h), (paste_x + new_w + mark, paste_y + new_h)], fill="#aaaaaa", width=1)

            # Page number
            if page_numbers:
                text = f"{page_idx + 1} / {total_pages}"
                try:
                    font = ImageFont.truetype("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 30)
                except Exception:
                    try:
                        font = ImageFont.truetype("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 30)
                    except Exception:
                        font = ImageFont.load_default()
                bbox = draw.textbbox((0, 0), text, font=font)
                tw = bbox[2] - bbox[0]
                draw.text(((w_px - tw) / 2, h_px - margin_px + 10), text, fill="#666666", font=font)

            rendered_images.append(canvas)

        # Save as multi-page PDF
        if rendered_images:
            first = rendered_images[0].convert("RGB")
            rest = [im.convert("RGB") for im in rendered_images[1:]]
            first.save(
                output_path,
                "PDF",
                resolution=300.0,
                save_all=True,
                append_images=rest,
            )

        # Cleanup docs
        for doc in docs:
            doc.close()

        return {
            "invoices_count": len(all_pages),
            "page_count": total_pages,
            "output_path": output_path,
            "layout": layout,
        }
