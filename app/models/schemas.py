from pydantic import BaseModel
from typing import Optional, List


class SplitRequest(BaseModel):
    mode: str  # "all", "ranges", "fixed"
    value: Optional[str] = None  # e.g., "1-3,5-10" or "2"


class MergeRequest(BaseModel):
    pass  # Files are uploaded via multipart


class TaskResponse(BaseModel):
    task_id: str
    status: str
    message: str
    download_url: str
    file_count: int


class DownloadResponse(BaseModel):
    task_id: str
    download_url: str
    expires_at: Optional[str] = None


class ErrorResponse(BaseModel):
    detail: str


class RedactPageMatches(BaseModel):
    page: int  # 1 基页码
    count: int


class RedactLocateResponse(BaseModel):
    """关键词只读定位结果——只含计数与页码分布，绝不回显原文。"""

    matches: List[RedactPageMatches]
    total: int
