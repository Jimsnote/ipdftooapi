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
from fastapi.concurrency import run_in_threadpool

from app.config import settings
from app.core.exceptions import FileTooLargeError
from app.core.file_security import make_task_dir, safe_join, save_upload_file, validate_extension, validate_file_header
from app.core.logger import get_logger
from app.models.schemas import TaskResponse
from app.services.ofd_validator import (
    OFD_MSG_ENCRYPTED,
    OFD_MSG_NOT_OFD,
    OfdEncryptedError,
    OfdFileError,
    convert_ofd_to_pdf,
    count_pdf_pages,
    validate_ofd_zip,
)
from app.services.pdf_to_image import PDFToImageConverter

router = APIRouter()
logger = get_logger(__name__)

TEMP_DIR = os.path.abspath("/app/temp" if os.path.exists("/app/temp") else "./temp")
os.makedirs(TEMP_DIR, exist_ok=True)

OFD_EXTENSIONS = (".ofd",)

# 错误三分类文案（docs/OFD_VIEWER_DESIGN.md §4.3 表）；not-ofd/encrypted
# 两类与 /ofd-to-pdf 共用常量（P3-5 集中在 ofd_validator，防止措辞漂移），
# convert-failed 为本端点专属文案保留在此
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
        raise HTTPException(status_code=422, detail=OFD_MSG_ENCRYPTED)
    except OfdFileError as e:
        logger.info(f"OFD view task {task_id} rejected: {e}")
        raise HTTPException(status_code=422, detail=OFD_MSG_NOT_OFD)

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


# ---------------------------------------------------------------------------
# POST /api/v1/ofd/to-image：OFD 逐页转图片（PNG/JPG）
# ---------------------------------------------------------------------------

# 页数护栏：文档类 OFD 可能上百页，超出引导拆分（渲染是全页 300DPI 重活）
MAX_TO_IMAGE_PAGES = 200
VALID_DPI = {"96", "150", "300"}


@router.post("/to-image", summary="Convert an OFD file to page images (PNG/JPG)")
async def ofd_to_image(
    file: UploadFile = File(...),
    format: str = File("jpg"),
    dpi: str = File("150"),
):
    """OFD → PDF（复用 /ofd/view 的进程池转换链，crop=False 保留文档物理
    尺寸语义）→ 逐页渲染（复用 PDFToImageConverter：像素预算护栏 + DPI
    三档 + 半成品清理）。单页直接下载，多页 ZIP。文档/发票通用。"""
    if dpi not in VALID_DPI:
        raise HTTPException(status_code=400, detail="DPI 仅支持 96 / 150 / 300")
    # 对抗审查 R1：format 白名单（此前 'svg'/'JPG'/'jpg;../x' 被静默当 PNG 放行）
    fmt_norm = (format or "").lower()
    if fmt_norm not in ("jpg", "png"):
        raise HTTPException(status_code=400, detail="格式仅支持 jpg / png")
    validate_ofd_upload_50mb(file)
    task_id, task_dir = make_task_dir(TEMP_DIR)

    input_path = save_upload_file(
        file,
        safe_join(task_dir, "input.ofd"),
        settings.MAX_UPLOAD_SIZE,
        OFD_EXTENSIONS,
    )

    try:
        validate_ofd_zip(input_path)
    except OfdEncryptedError:
        logger.info(f"OFD to-image task {task_id} rejected: encrypted file")
        raise HTTPException(status_code=422, detail=OFD_MSG_ENCRYPTED)
    except OfdFileError as e:
        logger.info(f"OFD to-image task {task_id} rejected: {e}")
        raise HTTPException(status_code=422, detail=OFD_MSG_NOT_OFD)

    # 中间产物 PDF 不能叫 converted.pdf——下载端点候选表按序匹配，
    # converted.pdf 排在 converted.jpg/png 与 images.zip 之前，会把这个
    # 中间文件当成最终产物返回（pytest 真实样本抓包实证）
    pdf_path = safe_join(task_dir, "render-src.pdf")
    try:
        success, result = await convert_ofd_to_pdf(input_path, pdf_path, crop=False)
    except Exception as e:
        logger.error(f"OFD to-image task {task_id} conversion failed: {e}")
        raise HTTPException(status_code=500, detail="服务器处理失败，请稍后重试")
    if not success:
        logger.error(f"OFD to-image task {task_id} conversion failed: {result}")
        raise HTTPException(status_code=422, detail=MSG_CONVERT_FAILED)

    try:
        page_count = await asyncio.to_thread(count_pdf_pages, pdf_path)
    except Exception as e:
        logger.error(f"OFD to-image task {task_id}: page count failed: {e}")
        raise HTTPException(status_code=422, detail=MSG_CONVERT_FAILED)
    if page_count > MAX_TO_IMAGE_PAGES:
        raise HTTPException(
            status_code=422,
            detail=f"该文档共 {page_count} 页，超过单次 {MAX_TO_IMAGE_PAGES} 页上限，请拆分后处理",
        )

    # 大文档降档：同步接口受网关超时约束（nginx 60s），均 6.8s/页的渲染
    # 速度下，30 页以上 300DPI、80 页以上 150DPI 必然超时——自动降档并
    # 在结果消息里说明（语料实测：p50=1 页、最大 148 页、11 个 >10 页）
    render_dpi = int(dpi)
    note = ""
    if page_count > 80 and render_dpi > 96:
        render_dpi = 96
        note = f"文档共 {page_count} 页，清晰度已自动降为 96 DPI 以保证转换完成"
    elif page_count > 30 and render_dpi > 150:
        render_dpi = 150
        note = f"文档共 {page_count} 页，清晰度已自动降为 150 DPI 以保证转换完成"

    img_format = fmt_norm
    try:
        converter = PDFToImageConverter(pdf_path)
        image_paths = await run_in_threadpool(
            converter.convert,
            output_dir=task_dir,
            format=img_format,
            dpi=render_dpi,
        )
    except ValueError as e:
        # 像素预算超限（超大页面 × 高 DPI）：给可操作的中文提示
        raise HTTPException(status_code=422, detail=f"{e}（可尝试更低的 DPI）")
    except Exception as e:
        logger.error(f"OFD to-image task {task_id} render failed: {e!r}")
        raise HTTPException(status_code=500, detail="服务器处理失败，请稍后重试")

    if not image_paths:
        raise HTTPException(status_code=422, detail="未能从该 OFD 渲染出任何页面")

    if len(image_paths) == 1:
        ext = os.path.splitext(image_paths[0])[1] or f".{img_format}"
        final_path = safe_join(task_dir, f"converted{ext}")
        os.replace(image_paths[0], final_path)
    else:
        final_path = safe_join(task_dir, "images.zip")
        PDFToImageConverter.zip_images(image_paths, final_path)

    logger.info(
        f"OFD to-image task {task_id} completed: {len(image_paths)} page(s), "
        f"format={img_format}, dpi={render_dpi} (requested={dpi})"
    )
    message = f"已转换 {len(image_paths)} 页图片（{img_format.upper()} @ {render_dpi} DPI）"
    if note:
        message += f"。{note}"
    return TaskResponse(
        task_id=task_id,
        status="completed",
        message=message,
        download_url=f"/api/v1/pdf/download/{task_id}",
        file_count=len(image_paths),
    )


def validate_ofd_upload_50mb(file: UploadFile) -> None:
    """转图端点用 50MB 上限（与 /ofd-to-pdf 对齐；查看器的 20MB 是预览场景）。"""
    if file.size and file.size > settings.MAX_UPLOAD_SIZE:
        raise FileTooLargeError(settings.MAX_UPLOAD_SIZE)
    suffix = validate_extension(file.filename, OFD_EXTENSIONS)
    validate_file_header(file, suffix)
