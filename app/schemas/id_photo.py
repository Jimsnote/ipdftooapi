from pydantic import BaseModel, Field
from typing import Literal, List


class CanvasImage(BaseModel):
    id: str
    x: float = Field(..., description="X position in millimeters")
    y: float = Field(..., description="Y position in millimeters")
    width: float = Field(..., description="Width in millimeters", gt=0)
    height: float = Field(..., description="Height in millimeters", gt=0)
    rotation: float = Field(0, description="Rotation in degrees")
    src: str = Field(..., description="Base64 encoded image data (data URL or raw base64)")


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
    opacity: float = Field(0.15, ge=0, le=1)
    enabled: bool = False


class IDPhotoRenderRequest(BaseModel):
    orientation: Literal["portrait", "landscape"] = "portrait"
    images: List[CanvasImage] = Field(default_factory=list)
    texts: List[CanvasText] = Field(default_factory=list)
    tiled_watermark: TiledWatermark = Field(default_factory=TiledWatermark)
