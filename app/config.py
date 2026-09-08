import json
from typing import Any, List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    APP_NAME: str = "iPDFToo API"
    APP_VERSION: str = "0.1.0"
    DEBUG: bool = False
    CORS_ORIGINS: List[str] = ["http://localhost:3000"]

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Any) -> List[str] | Any:
        if isinstance(value, str):
            raw_value = value.strip()
            if not raw_value:
                return []

            if raw_value.startswith("["):
                parsed = json.loads(raw_value)
                if isinstance(parsed, list):
                    return [str(item).strip() for item in parsed if str(item).strip()]

            return [item.strip() for item in raw_value.split(",") if item.strip()]

        if isinstance(value, (tuple, set)):
            return list(value)

        return value

    # COS
    COS_SECRET_ID: str = ""
    COS_SECRET_KEY: str = ""
    COS_REGION: str = "ap-guangzhou"
    COS_BUCKET: str = ""
    COS_UPLOAD_EXPIRE: int = 3600

    # Upload limits
    MAX_UPLOAD_SIZE: int = 50 * 1024 * 1024  # 50MB
    MAX_FILES_PER_REQUEST: int = 20

    # OFD viewer（docs/OFD_VIEWER_DESIGN.md：20MB 上限，数科官方 5MB 的 4 倍）
    OFD_VIEW_MAX_SIZE: int = 20 * 1024 * 1024

    # OCR
    OCR_MAX_SIZE: int = 20 * 1024 * 1024  # 20MB
    OCR_MAX_PAGES: int = 10
    OCR_MIN_IMG_DIM: int = 200
    OCR_ENGINE: str = "onnxruntime"  # "onnxruntime" (Linux) | "mkldnn_false" (Windows local test)


settings = Settings()
