import os
import uuid
from pathlib import Path
from typing import Iterable, Tuple

from fastapi import HTTPException, UploadFile, status

from app.core.exceptions import FileTooLargeError, InvalidFileTypeError

CHUNK_SIZE = 1024 * 1024

PDF_MAGIC = (b"%PDF-",)
ZIP_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
OLE_MAGIC = (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",)
JPEG_MAGIC = (b"\xff\xd8\xff",)
PNG_MAGIC = (b"\x89PNG\r\n\x1a\n",)
# WEBP 是 RIFF 容器：RIFF 头 + 偏移 8 处的 "WEBP" 标识
WEBP_MAGIC = (b"RIFF",)

OFFICE_OPENXML_EXTENSIONS = {".docx", ".pptx", ".xlsx"}
OFFICE_LEGACY_EXTENSIONS = {".doc", ".ppt", ".xls"}


def validate_uuid(task_id: str) -> str:
    """Return the canonical UUID string or reject unsafe task ids."""
    try:
        parsed = uuid.UUID(task_id, version=4)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="任务 ID 格式无效",
        )
    return str(parsed)


def safe_join(root: str, *parts: str) -> str:
    """Join paths and ensure the resolved path stays inside root."""
    root_path = Path(root).resolve()
    target = root_path.joinpath(*parts).resolve()
    try:
        target.relative_to(root_path)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="文件路径无效",
        )
    return str(target)


def get_task_dir(temp_dir: str, task_id: str, create: bool = False) -> str:
    canonical_id = validate_uuid(task_id)
    task_dir = safe_join(temp_dir, canonical_id)
    if create:
        os.makedirs(task_dir, exist_ok=True)
    return task_dir


def make_task_dir(temp_dir: str) -> Tuple[str, str]:
    task_id = str(uuid.uuid4())
    task_dir = get_task_dir(temp_dir, task_id, create=True)
    return task_id, task_dir



def validate_extension(filename: str | None, allowed_extensions: Iterable[str]) -> str:
    suffix = Path(filename or "").suffix.lower()
    allowed = {ext.lower() for ext in allowed_extensions}
    if suffix not in allowed:
        raise InvalidFileTypeError()
    return suffix


def validate_file_header(file: UploadFile, suffix: str) -> None:
    header = file.file.read(16)
    file.file.seek(0)

    suffix = suffix.lower()
    if suffix == ".pdf":
        _require_magic(header, PDF_MAGIC)
    elif suffix in OFFICE_OPENXML_EXTENSIONS or suffix == ".ofd":
        _require_magic(header, ZIP_MAGIC)
    elif suffix in OFFICE_LEGACY_EXTENSIONS:
        _require_magic(header, OLE_MAGIC)
    elif suffix in {".jpg", ".jpeg"}:
        _require_magic(header, JPEG_MAGIC)
    elif suffix == ".png":
        _require_magic(header, PNG_MAGIC)
    elif suffix == ".webp":
        # RIFF 容器 + 偏移 8 的 WEBP 标识（OCR 扫描件链路支持 webp）
        if not (header.startswith(b"RIFF") and len(header) >= 12 and header[8:12] == b"WEBP"):
            raise InvalidFileTypeError()
    else:
        raise InvalidFileTypeError()


def _record_upload_name(task_dir: str, filename: str | None) -> None:
    """把原始上传文件名追加记录到任务目录的 upload_names.txt（每行一个）。

    供下载端点把输出文件名还原为「原始文件主干 + 输出扩展名」。
    记录失败静默忽略，不影响主流程。
    """
    try:
        name = Path(filename or "").name.strip()
        if not name or name in {".", ".."}:
            return
        # 去掉不可打印字符，限制长度，避免异常文件名进入响应头
        name = "".join(ch for ch in name if ch.isprintable())[:200].strip()
        if not name:
            return
        with open(os.path.join(task_dir, "upload_names.txt"), "a", encoding="utf-8") as f:
            f.write(name + "\n")
    except Exception:
        pass


def save_upload_file(
    file: UploadFile,
    destination: str,
    max_size: int,
    allowed_extensions: Iterable[str],
) -> str:
    suffix = validate_extension(file.filename, allowed_extensions)
    if file.size and file.size > max_size:
        raise FileTooLargeError(max_size)
    validate_file_header(file, suffix)

    os.makedirs(os.path.dirname(destination), exist_ok=True)
    written = 0
    try:
        with open(destination, "wb") as out:
            while True:
                chunk = file.file.read(CHUNK_SIZE)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_size:
                    raise FileTooLargeError(max_size)
                out.write(chunk)
        _record_upload_name(os.path.dirname(destination), file.filename)
    except Exception:
        if os.path.exists(destination):
            os.remove(destination)
        raise
    finally:
        file.file.seek(0)
    return destination


def _require_magic(header: bytes, allowed_magic: Iterable[bytes]) -> None:
    if not any(header.startswith(magic) for magic in allowed_magic):
        raise InvalidFileTypeError()
