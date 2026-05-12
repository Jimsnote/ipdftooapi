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


class DownloadResponse(BaseModel):
    task_id: str
    download_url: str
    expires_at: Optional[str] = None


class ErrorResponse(BaseModel):
    detail: str
