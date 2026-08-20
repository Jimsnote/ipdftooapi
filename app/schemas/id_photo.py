from pydantic import BaseModel, Field
from typing import Dict, Literal, List, Optional


class CanvasImage(BaseModel):
    id: str
    x: float = Field(..., description="X position in millimeters")
    y: float = Field(..., description="Y position in millimeters")
    width: float = Field(..., description="Width in millimeters", gt=0)
    height: float = Field(..., description="Height in millimeters", gt=0)
    rotation: float = Field(0, description="Rotation in degrees")
    # 图片数据：优先用 src_ref 从 source_images 去重取，否则用内联 src。
    # 一键铺满 A4 时 49 张格子共享同一张原图，用 src_ref 可避免把 base64 发几十遍。
    src: Optional[str] = Field(None, description="Base64 image data (data URL or raw base64); omitted when src_ref is used")
    src_ref: Optional[str] = Field(None, description="Reference key into request.source_images for deduplicated storage")
    cover: bool = Field(False, description="When true, center-crop the source to the target aspect ratio (avoid distortion)")


class CanvasText(BaseModel):
    id: str
    x: float = Field(..., description="X position in millimeters")
    y: float = Field(..., description="Y position in millimeters")
    text: str
    font_size: float = Field(..., description="Font size in millimeters", gt=0)
    color: str = Field("#000000", description="Hex color")
    rotation: float = Field(0, description="Rotation in degrees")


class TiledWatermark(BaseModel):
    text: str = ""
    font_size: float = 5.0
    color: str = "#000000"
    opacity: float = Field(0.5, ge=0, le=1)
    enabled: bool = False


class IDPhotoRenderRequest(BaseModel):
    orientation: Literal["portrait", "landscape"] = "portrait"
    images: List[CanvasImage] = Field(default_factory=list)
    texts: List[CanvasText] = Field(default_factory=list)
    tiled_watermark: TiledWatermark = Field(default_factory=TiledWatermark)
    # 一键排版 / 裁切虚线相关
    source_images: Dict[str, str] = Field(default_factory=dict, description="Deduplicated image store: key -> base64 data URL")
    cut_lines: bool = Field(True, description="Draw light dashed crop lines around each image cell")
    spec_key: Optional[str] = Field(None, description="Selected ID photo spec key (for record)")
    margin_mm: float = Field(5.0, description="Page margin in mm (used by spec auto-layout)")
    grid_gap_mm: float = Field(2.0, description="Gap between cells in mm (used by spec auto-layout)")
