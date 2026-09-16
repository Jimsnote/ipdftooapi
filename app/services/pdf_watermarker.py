"""PDF 水印服务（文字/图片印章）。

用 PyMuPDF 在每页上层（overlay）绘制印章：
- 文字：insert_text + morph 任意角旋转（45° 斜角），CJK 用内置 china-s 字体
- 图片：insert_image 本身不支持透明度——先把图片用 PIL 预乘 alpha 通道
  （转 RGBA、alpha 乘 opacity）再插入，视觉透明度等效
- 布局：tile（网格平铺）/ center（页面居中单个）
- 印章只加不清：与"涂黑真删除"不同，水印是覆盖层，不修改原内容
"""

import io
import math
import os
from typing import List, Optional, Sequence, Set, Tuple

import fitz
from PIL import Image

from app.core.logger import get_logger

logger = get_logger(__name__)

# 与 coolpdf 对齐的取值范围：过淡看不见、过浓遮内容
MIN_OPACITY = 0.05
MAX_OPACITY = 0.5

TEXT_COLORS = {
    "gray": (0.5, 0.5, 0.5),
    "red": (0.85, 0.15, 0.15),
    "blue": (0.15, 0.35, 0.75),
    "black": (0.0, 0.0, 0.0),
}

ROTATION_DEG = 45.0
_FONT = "china-s"  # 内置 CJK 字体（Droid Sans Fallback）


def _rotation_matrix() -> fitz.Matrix:
    rad = math.radians(ROTATION_DEG)
    return fitz.Matrix(math.cos(rad), math.sin(rad), -math.sin(rad), math.cos(rad), 0, 0)


def _clamp_opacity(opacity: float) -> float:
    return min(MAX_OPACITY, max(MIN_OPACITY, opacity))


def _text_width(text: str, fontsize: float) -> float:
    """CJK 字符宽 ≈ fontsize，ASCII ≈ 0.55 * fontsize（用于居中反推基点）。"""
    width = 0.0
    for ch in text:
        width += fontsize if ord(ch) > 0x2E7F else fontsize * 0.55
    return width


def _prepare_image_bytes(image_bytes: bytes, opacity: float, rotation_deg: float = 0.0) -> bytes:
    """把图片转 PNG 并预乘透明度（insert_image 不支持 opacity 参数）。

    旋转也在 PIL 内完成（insert_image 只支持 90° 倍数、无 morph）：
    expand=True 让画布扩到旋转后的 bounding box，透明背景不遮内容。
    """
    img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
    alpha = img.getchannel("A").point(lambda a: int(a * _clamp_opacity(opacity)))
    img.putalpha(alpha)
    if rotation_deg % 360 != 0:
        # PIL rotate 为逆时针，取负号与文字水印的顺时针 45° 同向
        img = img.rotate(-rotation_deg, expand=True, resample=Image.BICUBIC)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class PDFWatermarker:
    """Add text or image watermarks to a PDF file."""

    def __init__(self, input_path: str):
        self.input_path = input_path
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"Input file not found: {input_path}")

    @property
    def total_pages(self) -> int:
        with fitz.open(self.input_path) as doc:
            return len(doc)

    def watermark_text(
        self,
        text: str,
        output_path: str,
        opacity: float = 0.2,
        layout: str = "tile",
        color: str = "gray",
        fontsize: float = 48.0,
        pages: Optional[Set[int]] = None,
    ) -> Tuple[int, int]:
        """加文字水印。返回 (总页数, 处理页数)。"""
        if not text or not text.strip():
            raise ValueError("水印文字不能为空")
        if len(text) > 60:
            raise ValueError("水印文字过长（最多 60 字符）")
        if color not in TEXT_COLORS:
            raise ValueError("不支持的水印颜色")
        if layout not in ("tile", "center"):
            raise ValueError("水印布局只支持 tile / center")
        if fontsize < 8 or fontsize > 120:
            raise ValueError("水印字号须在 8-120 之间")

        rgb = TEXT_COLORS[color]
        doc = fitz.open(self.input_path)
        try:
            targets = self._targets(doc, pages)
            matrix = _rotation_matrix()
            stamp_w = _text_width(text, fontsize)
            stamp_h = fontsize

            for index in targets:
                page = doc[index]
                for cx, cy in self._anchor_points(page.rect, layout, stamp_w, stamp_h):
                    # insert_text 的 point 是基线左端：先放到"印章中心"，
                    # 再以该点为 pivot 旋转 45°
                    point = fitz.Point(cx - stamp_w / 2, cy + stamp_h / 2)
                    page.insert_text(
                        point,
                        text,
                        fontsize=fontsize,
                        fontname=_FONT,
                        color=rgb,
                        fill_opacity=_clamp_opacity(opacity),
                        morph=(point, matrix),
                    )

            doc.save(output_path, garbage=3, deflate=True)
            logger.info(
                f"Text watermark applied to {len(targets)} page(s): {self.input_path} -> {output_path}"
            )
            return len(doc), len(targets)
        finally:
            doc.close()

    def watermark_image(
        self,
        image_bytes: bytes,
        output_path: str,
        opacity: float = 0.2,
        layout: str = "tile",
        width_fraction: float = 0.3,
        pages: Optional[Set[int]] = None,
    ) -> Tuple[int, int]:
        """加图片水印。width_fraction = 印章宽度占页宽比例（0.1-0.8）。"""
        if not image_bytes:
            raise ValueError("水印图片为空")
        if layout not in ("tile", "center"):
            raise ValueError("水印布局只支持 tile / center")
        if width_fraction < 0.1 or width_fraction > 0.8:
            raise ValueError("图片宽度占比须在 0.1-0.8 之间")

        prepared = _prepare_image_bytes(image_bytes, opacity, ROTATION_DEG)
        doc = fitz.open(self.input_path)
        try:
            targets = self._targets(doc, pages)

            # 内容尺寸按用户意图（宽度占比 × 页宽）计算，再换算旋转后
            # 的 bounding box（PIL 已 expand，插入矩形 = bbox）
            rad = math.radians(ROTATION_DEG)
            cos_v, sin_v = abs(math.cos(rad)), abs(math.sin(rad))
            with Image.open(io.BytesIO(image_bytes)) as im:
                iw, ih = im.size
            content_w = None
            content_h = None

            for index in targets:
                page = doc[index]
                prect = page.rect
                if content_w is None:
                    content_w = prect.width * width_fraction
                    content_h = content_w * ih / iw
                bbox_w = content_w * cos_v + content_h * sin_v
                bbox_h = content_w * sin_v + content_h * cos_v

                for cx, cy in self._anchor_points(prect, layout, bbox_w, bbox_h):
                    rect = fitz.Rect(
                        cx - bbox_w / 2, cy - bbox_h / 2, cx + bbox_w / 2, cy + bbox_h / 2
                    )
                    page.insert_image(rect, stream=prepared, overlay=True)

            doc.save(output_path, garbage=3, deflate=True)
            logger.info(
                f"Image watermark applied to {len(targets)} page(s): {self.input_path} -> {output_path}"
            )
            return len(doc), len(targets)
        finally:
            doc.close()

    @staticmethod
    def _targets(doc: fitz.Document, pages: Optional[Set[int]]) -> List[int]:
        """解析目标页（0 基列表）；pages=None = 全部。"""
        if pages is None:
            return list(range(len(doc)))
        targets = sorted(p - 1 for p in pages)
        targets = [i for i in targets if 0 <= i < len(doc)]
        if not targets:
            raise ValueError("未指定有效的水印页码")
        return targets

    @staticmethod
    def _anchor_points(
        prect: fitz.Rect, layout: str, stamp_w: float, stamp_h: float
    ) -> Sequence[Tuple[float, float]]:
        """印章中心点集合。tile 按印章尺寸的 2 倍步长铺满整页（含出页边，
        旋转后的角落在页内也能覆盖到）；center 只取页面中心。"""
        if layout == "center":
            return [(prect.width / 2, prect.height / 2)]

        step_x = max(stamp_w * 2.0, 120.0)
        step_y = max(stamp_h * 4.0, 120.0)
        points: List[Tuple[float, float]] = []
        y = 0.0
        row = 0
        while y <= prect.height + stamp_h:
            # 奇数行水平错位半个步长，铺出来更像传统水印
            offset = step_x / 2 if row % 2 == 1 else 0.0
            x = offset
            while x <= prect.width + stamp_w:
                points.append((x, y))
                x += step_x
            y += step_y
            row += 1
        if not points:
            points.append((prect.width / 2, prect.height / 2))
        return points

    @staticmethod
    def parse_page_list(pages_str: str, total_pages: int) -> Set[int]:
        """解析页码串（1 基），语义同 PDFPageRemover.parse_page_list。"""
        result: Set[int] = set()
        parts = [p.strip() for p in pages_str.split(",") if p.strip()]

        for part in parts:
            if "-" in part:
                start, end = part.split("-", 1)
                start_num = int(start.strip())
                end_num = int(end.strip())
                if start_num > end_num:
                    raise ValueError(f"页码区间无效：{part}")
                for p in range(start_num, end_num + 1):
                    if 1 <= p <= total_pages:
                        result.add(p)
            else:
                p = int(part.strip())
                if 1 <= p <= total_pages:
                    result.add(p)

        return result
