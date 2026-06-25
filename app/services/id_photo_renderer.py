import base64
import io
import math
import os
from typing import List, Tuple

from PIL import Image, ImageDraw, ImageFont

from app.core.logger import get_logger
from app.schemas.id_photo import CanvasImage, CanvasText, TiledWatermark

logger = get_logger(__name__)

A4_WIDTH_MM = 210.0
A4_HEIGHT_MM = 297.0
DPI = 300
IMAGE_MAGIC_PREFIXES = (
    b"\xff\xd8\xff",
    b"\x89PNG\r\n\x1a\n",
)

_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/arial.ttf",
]


def _load_font(size_px: float) -> ImageFont.FreeTypeFont:
    for path in _FONT_CANDIDATES:
        if path and os.path.exists(path):
            try:
                return ImageFont.truetype(path, int(size_px))
            except Exception:
                continue
    logger.warning("No suitable TTF font found, using default bitmap font")
    return ImageFont.load_default()


def _mm_to_px(mm: float) -> int:
    return int(round(mm * DPI / 25.4))


def _parse_base64(src: str) -> bytes:
    if src.startswith("data:"):
        src = src.split(",", 1)[-1]
    return base64.b64decode(src.strip())


def _validate_image_header(data: bytes) -> None:
    if not any(data.startswith(prefix) for prefix in IMAGE_MAGIC_PREFIXES):
        raise ValueError("\u4ec5\u652f\u6301 JPG \u6216 PNG \u56fe\u7247")


def _hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    hex_color = hex_color.lstrip("#")
    if len(hex_color) == 3:
        hex_color = "".join(c * 2 for c in hex_color)
    return tuple(int(hex_color[i : i + 2], 16) for i in (0, 2, 4))


def _compute_a4_size(orientation: str) -> Tuple[int, int]:
    if orientation == "landscape":
        return (_mm_to_px(A4_HEIGHT_MM), _mm_to_px(A4_WIDTH_MM))
    return (_mm_to_px(A4_WIDTH_MM), _mm_to_px(A4_HEIGHT_MM))


class IDPhotoRenderer:
    def __init__(self, orientation: str = "portrait"):
        self.orientation = orientation
        self.width_px, self.height_px = _compute_a4_size(orientation)
        self.canvas = Image.new("RGB", (self.width_px, self.height_px), "white")
        self.draw = ImageDraw.Draw(self.canvas)

    def _render_tiled_watermark(self, watermark: TiledWatermark) -> None:
        if not watermark.enabled or not watermark.text:
            return

        font_size_px = max(1, _mm_to_px(watermark.font_size))
        font = _load_font(font_size_px)
        text = watermark.text
        rgb = _hex_to_rgb(watermark.color)
        # Pre-blend the watermark color against a white background so that
        # we can draw with alpha=255 and avoid the "double-blending" issue
        # that makes low-opacity watermarks invisible on white paper.
        blended = tuple(int(c * (1 - watermark.opacity) + 255 * watermark.opacity) for c in rgb)

        bbox = font.getbbox(text)
        text_w = max(1, bbox[2] - bbox[0])
        text_h = max(1, bbox[3] - bbox[1])

        diag = int(math.ceil(math.sqrt(text_w**2 + text_h**2))) + font_size_px * 2
        tile_size = diag * 3
        tile = Image.new("RGBA", (tile_size, tile_size), (255, 255, 255, 0))
        tile_draw = ImageDraw.Draw(tile)
        tile_draw.text(
            (tile_size // 2 - text_w // 2, tile_size // 2 - text_h // 2),
            text,
            font=font,
            fill=(*blended, 255),
        )
        tile = tile.rotate(45, expand=False, resample=Image.BICUBIC)

        step = int(tile_size * 0.55)
        y = -tile_size
        while y < self.height_px + tile_size:
            x = -tile_size
            while x < self.width_px + tile_size:
                self.canvas.paste(tile.convert("RGB"), (x, y), tile.split()[3])
                x += step
            y += step

    def _render_image(self, img: CanvasImage) -> None:
        try:
            data = _parse_base64(img.src)
            _validate_image_header(data)
            source = Image.open(io.BytesIO(data)).convert("RGBA")
        except ValueError:
            raise
        except Exception as e:
            logger.warning(f"Failed to decode image {img.id}: {e}")
            return

        w_px = max(1, _mm_to_px(img.width))
        h_px = max(1, _mm_to_px(img.height))
        x_px = _mm_to_px(img.x)
        y_px = _mm_to_px(img.y)

        resized = source.resize((w_px, h_px), Image.LANCZOS)

        if abs(img.rotation % 360) > 0.1:
            rotated = resized.rotate(-img.rotation, expand=True, resample=Image.BICUBIC)
        else:
            rotated = resized

        self.canvas.paste(rotated, (x_px, y_px), rotated)

    def _render_text(self, text_obj: CanvasText) -> None:
        font_size_px = max(1, _mm_to_px(text_obj.font_size))
        font = _load_font(font_size_px)
        fill = _hex_to_rgb(text_obj.color)
        x_px = _mm_to_px(text_obj.x)
        y_px = _mm_to_px(text_obj.y)

        if abs(text_obj.rotation % 360) > 0.1:
            bbox = font.getbbox(text_obj.text)
            text_w = max(1, bbox[2] - bbox[0])
            text_h = max(1, bbox[3] - bbox[1])
            # Use a slightly larger canvas to avoid clipping descenders
            pad = font_size_px
            txt_img = Image.new("RGBA", (text_w + pad * 2, text_h + pad * 2), (255, 255, 255, 0))
            txt_draw = ImageDraw.Draw(txt_img)
            txt_draw.text((pad, pad), text_obj.text, font=font, fill=(*fill, 255))
            rotated = txt_img.rotate(-text_obj.rotation, expand=True, resample=Image.BICUBIC)
            self.canvas.paste(rotated, (x_px, y_px), rotated)
        else:
            self.draw.text((x_px, y_px), text_obj.text, font=font, fill=fill)

    def render(
        self,
        images: List[CanvasImage],
        texts: List[CanvasText],
        tiled_watermark: TiledWatermark,
        output_path: str,
    ) -> str:
        for img in images:
            self._render_image(img)

        for txt in texts:
            self._render_text(txt)

        # Render tiled watermark on top so it covers photos and text
        self._render_tiled_watermark(tiled_watermark)

        self.canvas.save(
            output_path,
            "PDF",
            resolution=DPI,
            title="iPDFToo ID Photo Layout",
        )
        return output_path


class IDPhotoRendererService:
    @staticmethod
    def render_to_file(request, output_path: str) -> str:
        renderer = IDPhotoRenderer(orientation=request.orientation)
        renderer.render(
            images=request.images,
            texts=request.texts,
            tiled_watermark=request.tiled_watermark,
            output_path=output_path,
        )
        return output_path
