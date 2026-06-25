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
    else:
        raise InvalidFileTypeError()


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
