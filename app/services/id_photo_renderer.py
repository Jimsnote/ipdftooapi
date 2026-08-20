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
            # 前端 Konva 绕元素左上角（本地原点）旋转。为避免 PIL 默认 "绕中心旋转 + expand 居中"
            # 带来的锚点偏移，这里用显式仿射：先绕左上角旋转，再按包围盒平移对齐到 (x_px, y_px)。
            angle = math.radians(img.rotation)
            cos_t, sin_t = math.cos(angle), math.sin(angle)
            # 四个角旋转后（不含平移）的包围盒
            pts = [(0.0, 0.0), (w_px, 0.0), (w_px, h_px), (0.0, h_px)]
            proj = [(x * cos_t - y * sin_t, x * sin_t + y * cos_t) for x, y in pts]
            min_x = min(p[0] for p in proj)
            max_x = max(p[0] for p in proj)
            min_y = min(p[1] for p in proj)
            max_y = max(p[1] for p in proj)
            out_w = int(math.ceil(max_x - min_x))
            out_h = int(math.ceil(max_y - min_y))
            # PIL 的 AFFINE 参数必须是「输出->源」的逆变换矩阵。
            # 前向（本地->旋转后）：X = x·cosθ - y·sinθ, Y = x·sinθ + y·cosθ
            # 逆变换（旋转后->本地）：x = X·cosθ + Y·sinθ, y = -X·sinθ + Y·cosθ
            # 输出像素 (u,v) 对应旋转坐标 (min_x+u, min_y+v)，代入逆变换得到源坐标。
            rotated = resized.convert("RGBA").transform(
                (out_w, out_h),
                Image.AFFINE,
                (
                    cos_t,
                    sin_t,
                    cos_t * min_x + sin_t * min_y,
                    -sin_t,
                    cos_t,
                    -sin_t * min_x + cos_t * min_y,
                ),
            )
            # 原图左上角最终落在画布 (x_px, y_px)
            self.canvas.paste(
                rotated,
                (int(round(x_px + min_x)), int(round(y_px + min_y))),
                rotated,
            )
        else:
            self.canvas.paste(resized, (x_px, y_px), resized)

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
