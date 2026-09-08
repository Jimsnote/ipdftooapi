"""OFD 在线查看路由。

设计依据：docs/OFD_VIEWER_DESIGN.md v1.1
- POST /api/v1/ofd/view：服务端 OFD→PDF（复用 ofd_converter.py 全部渲染补丁），
  前端 PDF.js 渲染。返回协议（§4.1，P2-6 对齐后）：
  { "task_id": "<uuid4>", "page_count": N, "pdf_url": "/api/v1/pdf/download/<task_id>",
    "status": "completed", "message": "...", "download_url": "...", "file_count": 1 }
  其中 status/message/download_url/file_count 与 /ofd-to-pdf 的 TaskResponse
  同名同义；pdf_url/page_count 为查看器专用扩展字段。
- pdf_url 复用现有下载端点（48-72h 随 temp 清理失效，UUID v4 不可枚举）。
- 错误三分类（§4.3）：预扫描拦截 / 转换失败（损坏或版式不支持）/ 加密。
"""

import asyncio
import os

from fastapi import APIRouter, File, HTTPException, UploadFile

from app.config import settings
from app.core.exceptions import FileTooLargeError
from app.core.file_security import make_task_dir, safe_join, save_upload_file, validate_extension, validate_file_header
from app.core.logger import get_logger
from app.services.ofd_validator import (
    OfdEncryptedError,
    OfdFileError,
    convert_ofd_to_pdf,
    count_pdf_pages,
    validate_ofd_zip,
)

router = APIRouter()
logger = get_logger(__name__)

TEMP_DIR = os.path.abspath("/app/temp" if os.path.exists("/app/temp") else "./temp")
os.makedirs(TEMP_DIR, exist_ok=True)

OFD_EXTENSIONS = (".ofd",)

# 错误三分类文案（docs/OFD_VIEWER_DESIGN.md §4.3 表）
MSG_NOT_OFD = "该文件不是有效的 OFD 文件（ZIP 结构校验失败）"
MSG_ENCRYPTED = "该 OFD 文件已加密，暂不支持在线查看，请先解密或使用官方阅读器"
MSG_CONVERT_FAILED = (
    "OFD 转换失败：该文件可能包含不受支持的电子签章或版式特性，"
    "请确认其为标准 OFD 文件后重试。"
)


def validate_ofd_upload(file: UploadFile) -> None:
    """查看器专用上传校验：20MB 上限 + 扩展名 + ZIP 魔数。"""
    if file.size and file.size > settings.OFD_VIEW_MAX_SIZE:
        raise FileTooLargeError(settings.OFD_VIEW_MAX_SIZE)
    suffix = validate_extension(file.filename, OFD_EXTENSIONS)
    validate_file_header(file, suffix)


@router.post("/view", summary="Convert an OFD file to PDF for online viewing")
async def ofd_view(file: UploadFile = File(...)):
    validate_ofd_upload(file)
    task_id, task_dir = make_task_dir(TEMP_DIR)

    input_path = save_upload_file(
        file,
        safe_join(task_dir, "input.ofd"),
        settings.OFD_VIEW_MAX_SIZE,
        OFD_EXTENSIONS,
    )

    # 预扫描（zip bomb 四规则 + 加密检测），在 easyofd 之前执行
    try:
        validate_ofd_zip(input_path)
    except OfdEncryptedError:
        logger.info(f"OFD view task {task_id} rejected: encrypted file")
        raise HTTPException(status_code=422, detail=MSG_ENCRYPTED)
    except OfdFileError as e:
        logger.info(f"OFD view task {task_id} rejected: {e}")
        raise HTTPException(status_code=422, detail=MSG_NOT_OFD)

    output_path = safe_join(task_dir, "converted.pdf")
    try:
        success, result = await convert_ofd_to_pdf(input_path, output_path)
    except Exception as e:
        logger.error(f"OFD view task {task_id} failed: {e}")
        raise HTTPException(status_code=500, detail="服务器处理失败，请稍后重试")

    if not success:
        logger.error(f"OFD view task {task_id} failed: {result}")
        raise HTTPException(status_code=422, detail=MSG_CONVERT_FAILED)

    try:
        # P2-2：fitz 解析为同步阻塞操作，移入线程避免卡住事件循环
        page_count = await asyncio.to_thread(count_pdf_pages, output_path)
    except Exception as e:
        # 输出 PDF 已生成但页数统计异常（极罕见）：降级为 1，前端加载真实
        # 文档后会用 doc.numPages 覆盖该占位值
        logger.error(f"OFD view task {task_id}: page count failed: {e}")
        page_count = 1
    logger.info(f"OFD view task {task_id} completed: {page_count} page(s)")
    return {
        "task_id": task_id,
        "page_count": page_count,
        "pdf_url": f"/api/v1/pdf/download/{task_id}",
        # P2-6 协议对齐：补齐 TaskResponse 同名字段（status/message/
        # download_url/file_count），前端不再需要按端点区分两套解析；
        # pdf_url/page_count 为查看器专用扩展字段，保留不变
        "status": "completed",
        "message": "OFD 文件已成功转换为 PDF",
        "download_url": f"/api/v1/pdf/download/{task_id}",
        "file_count": 1,
    }
