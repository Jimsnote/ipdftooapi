from pydantic import BaseModel, Field, model_validator
from typing import Dict, Literal, List, Optional

# 单张图片 base64 上限（约 15MB 解码后体积）+ 请求总图片体积上限
MAX_IMAGE_B64_CHARS = 20 * 1024 * 1024
MAX_TOTAL_IMAGE_B64_CHARS = 60 * 1024 * 1024


class CanvasImage(BaseModel):
    id: str
    x: float = Field(..., description="X position in millimeters")
    y: float = Field(..., description="Y position in millimeters")
    width: float = Field(..., description="Width in millimeters", gt=0)
    height: float = Field(..., description="Height in millimeters", gt=0)
    rotation: float = Field(0, description="Rotation in degrees")
    # 图片数据：优先用 src_ref 从 source_images 去重取，否则用内联 src。
    # 一键铺满 A4 时 49 张格子共享同一张原图，用 src_ref 可避免把 base64 发几十遍。
    src: Optional[str] = Field(None, description="Base64 image data (data URL or raw base64); omitted when src_ref is used", max_length=MAX_IMAGE_B64_CHARS)
    src_ref: Optional[str] = Field(None, description="Reference key into request.source_images for deduplicated storage")
    cover: bool = Field(False, description="When true, center-crop the source to the target aspect ratio (avoid distortion)")
    # 规格内裁剪 / 构图：在 cover 基础上再做缩放与平移，修正"头大身子小"等构图问题。
    # crop_zoom > 1 表示放大裁剪（取更小区域）；crop_x/crop_y ∈ [-0.5, 0.5] 表示在 cover 框内平移。
    crop_zoom: float = Field(1.0, description="Zoom-in factor for in-spec cropping (1 = no zoom)")
    crop_x: float = Field(0.0, description="Horizontal pan within cover crop box, -0.5..0.5")
    crop_y: float = Field(0.0, description="Vertical pan within cover crop box, -0.5..0.5")


class CanvasText(BaseModel):
    id: str
    x: float = Field(..., description="X position in millimeters")
    y: float = Field(..., description="Y position in millimeters")
    text: str = Field(..., max_length=1000, description="Text content")
    font_size: float = Field(..., description="Font size in millimeters", gt=0)
    color: str = Field("#000000", description="Hex color")
    rotation: float = Field(0, description="Rotation in degrees")


class TiledWatermark(BaseModel):
    text: str = Field("", max_length=200)
    font_size: float = Field(5.0, gt=0, le=50)
    color: str = "#000000"
    opacity: float = Field(0.5, ge=0, le=1)
    enabled: bool = False


class IDPhotoRenderRequest(BaseModel):
    orientation: Literal["portrait", "landscape"] = "portrait"
    images: List[CanvasImage] = Field(default_factory=list, max_length=200)
    texts: List[CanvasText] = Field(default_factory=list, max_length=200)
    tiled_watermark: TiledWatermark = Field(default_factory=TiledWatermark)
    # 一键排版 / 裁切虚线相关
    source_images: Dict[str, str] = Field(default_factory=dict, description="Deduplicated image store: key -> base64 data URL")
    cut_lines: bool = Field(True, description="Draw light dashed crop lines around each image cell")
    spec_key: Optional[str] = Field(None, description="Selected ID photo spec key (for record)")
    margin_mm: float = Field(5.0, description="Page margin in mm (used by spec auto-layout)")
    grid_gap_mm: float = Field(2.0, description="Gap between cells in mm (used by spec auto-layout)")
    # dpi 有硬上限：A4 画布像素数随 dpi 平方增长，无上限等于 OOM DoS
    dpi: int = Field(300, ge=100, le=600, description="Output DPI for the rendered PDF (300 or 600)")

    @model_validator(mode="after")
    def _check_image_payload_size(self):
        total = sum(len(v) for v in self.source_images.values())
        if total > MAX_TOTAL_IMAGE_B64_CHARS:
            raise ValueError("图片总数据量过大，请压缩图片或减少数量后重试")
        if len(self.source_images) > 200:
            raise ValueError("图片数量过多（最多 200 张）")
        return self
