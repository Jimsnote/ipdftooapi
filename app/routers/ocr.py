"""OCR router: scan-to-pdf 端点 + 下载。"""
import os
import threading

import fitz
from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import FileResponse

from app.core.file_security import (
    make_task_dir,
    safe_join,
    save_upload_file,
    validate_extension,
    get_task_dir,
)
from app.core.logger import get_logger
from app.models.schemas import TaskResponse
from app.services.ocr.precheck import precheck, MAX_SIZE

router = APIRouter()
logger = get_logger(__name__)

TEMP_DIR = os.path.abspath("/app/temp" if os.path.exists("/app/temp") else "./temp")
os.makedirs(TEMP_DIR, exist_ok=True)

OCR_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".webp")
IMG_EXTS = (".png", ".jpg", ".jpeg", ".webp")

# 全局锁：限并发=1，防 2GB 服务器 OOM
_ocr_lock = threading.Lock()


def _pdf_to_images(pdf_path: str, task_dir: str):
    """PDF 拆页成 PNG（200 DPI）。"""
    doc = fitz.open(pdf_path)
    paths = []
    for i, page in enumerate(doc):
        pix = page.get_pixmap(dpi=200)
        p = safe_join(task_dir, f"page_{i}.png")
        pix.save(p)
        paths.append(p)
    doc.close()
    return paths


@router.post("/scan-to-pdf", response_model=TaskResponse, summary="扫描件/图片转可搜索 PDF")
def scan_to_pdf(file: UploadFile = File(...)):
    suffix = validate_extension(file.filename, OCR_EXTS)
    is_image = suffix in IMG_EXTS
    task_id, task_dir = make_task_dir(TEMP_DIR)

    input_path = save_upload_file(
        file, safe_join(task_dir, f"input{suffix}"), MAX_SIZE, OCR_EXTS
    )

    # 预检
    ok, msg, has_text = precheck(input_path, is_image)
    if not ok:
        raise HTTPException(status_code=422, detail=msg)
    if has_text:
        raise HTTPException(
            status_code=400,
            detail="本文件已含可复制文字，无需 OCR。请直接使用 PDF 转 Word/Markdown 工具，或上传扫描件/图片。",
        )
    blurry = msg == "blurry"  # 模糊警告（不阻断）

    try:
        # 拆页成图
        if is_image:
            image_paths = [input_path]
        else:
            image_paths = _pdf_to_images(input_path, task_dir)

        # OCR（限并发=1）
        from app.services.ocr.engine import ocr_image
        from app.services.ocr.scan_to_pdf import build_searchable_pdf

        with _ocr_lock:
            ocr_results = [ocr_image(p) for p in image_paths]
            output_path = safe_join(task_dir, "searchable.pdf")
            build_searchable_pdf(image_paths, ocr_results, output_path)

        download_url = f"/api/v1/ocr/download/{task_id}"
        total_chars = sum(len(t) for r in ocr_results for _, t in (r or []))
        message = f"已生成可搜索 PDF，共 {len(image_paths)} 页，识别 {total_chars} 字符"
        if blurry:
            message += "（提示：图片较模糊，识别精度可能下降，建议上传 300DPI 以上清晰原图）"
        logger.info(f"scan-to-pdf task {task_id}: {len(image_paths)} pages, {total_chars} chars, blurry={blurry}")
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message=message,
            download_url=download_url,
            file_count=1,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"scan-to-pdf task {task_id} failed: {e}")
        raise HTTPException(status_code=500, detail="OCR 处理失败，请重试或更换文件")


@router.get("/download/{task_id}", summary="下载 OCR 结果")
def download(task_id: str):
    task_dir = get_task_dir(TEMP_DIR, task_id)
    if not os.path.exists(task_dir):
        raise HTTPException(status_code=404, detail="任务不存在或已过期")
    path = safe_join(task_dir, "searchable.pdf")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="结果文件不存在")
    return FileResponse(path, media_type="application/pdf", filename="searchable.pdf")
