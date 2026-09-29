"""渲染像素预算护栏（2026-09-29 审计 #2/#3 家族修复）。

背景：2GB 生产机上，超大页面/超大图片一次性渲染会直接 OOM 杀 worker
（20000×20000pt 页 @150dpi ≈ 4.9GB pixmap；506KB PNG 可解压出 1.69 亿像素）。
统一在此收敛"渲染前先算像素账"的检查，超限抛 ValueError（路由层映射 400/422，
fail-closed，绝不带病渲染）。
"""
import math

from app.core.logger import get_logger

logger = get_logger(__name__)

# 单边像素上限（约 A4@250dpi 的 3.4 倍余量，正常业务远达不到）
MAX_RENDER_SIDE_PX = 8192
# 单页像素预算：8192×8192（RGB pixmap + PIL 拷贝峰值约 400MB，2GB 机可承受）
MAX_RENDER_AREA_PX = 67_108_864

# 图片解压像素上限（防"解压炸弹"：几百 KB 的 PNG 可声明出数亿像素）
MAX_IMAGE_PIXELS = 67_108_864


def ensure_page_pixel_budget(
    width_pt: float, height_pt: float, dpi: int, what: str = "页面"
) -> None:
    """PDF 页面按目标 dpi 渲染前的像素预算校验。

    任一超限 → ValueError（调用方路由层统一映射 400，中文提示）。
    """
    if not math.isfinite(width_pt) or not math.isfinite(height_pt):
        raise ValueError(f"{what}尺寸无效")
    zoom = dpi / 72.0
    w_px = width_pt * zoom
    h_px = height_pt * zoom
    if max(w_px, h_px) > MAX_RENDER_SIDE_PX or w_px * h_px > MAX_RENDER_AREA_PX:
        raise ValueError(
            f"{what}尺寸过大（约 {int(w_px)}×{int(h_px)} 像素），超出处理上限，"
            "请先缩小页面尺寸或降低分辨率后重试"
        )


def ensure_image_pixel_limit(width: int, height: int, what: str = "图片") -> None:
    """图片解码前的像素总量校验（防解压炸弹，在 Image.open 之后、decode 之前调用）。"""
    try:
        pixels = int(width) * int(height)
    except (TypeError, ValueError):
        raise ValueError(f"{what}尺寸无效")
    if pixels > MAX_IMAGE_PIXELS:
        raise ValueError(
            f"{what}尺寸过大（{width}×{height}，约 {pixels // 1_000_000} 百万像素），"
            "超出处理上限，请缩小图片后重试"
        )
