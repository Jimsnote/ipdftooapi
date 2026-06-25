import os
from typing import List
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse

from app.models.schemas import TaskResponse
from app.services.invoice_merger import InvoiceMerger
from app.core.logger import get_logger
from app.core.file_security import get_task_dir, make_task_dir, safe_join, save_upload_file

router = APIRouter()
logger = get_logger(__name__)

TEMP_DIR = os.path.abspath("/app/temp" if os.path.exists("/app/temp") else "./temp")
os.makedirs(TEMP_DIR, exist_ok=True)

MAX_INVOICE_FILES = 50
MAX_INVOICE_SIZE = 10 * 1024 * 1024  # 10MB per file
PDF_EXTENSIONS = (".pdf",)


@router.post("/analyze", summary="Analyze uploaded invoice PDFs and return dimensions")
async def analyze_invoices(
    files: List[UploadFile] = File(...),
):
    if len(files) > MAX_INVOICE_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"最多上传 {MAX_INVOICE_FILES} 张发票",
        )

    task_id, task_dir = make_task_dir(TEMP_DIR)

    saved_paths = []
    original_names = []
    for index, f in enumerate(files):
        path = save_upload_file(
            f,
            safe_join(task_dir, f"invoice_{index + 1:03d}.pdf"),
            MAX_INVOICE_SIZE,
            PDF_EXTENSIONS,
        )
        saved_paths.append(path)
        original_names.append(f.filename or f"invoice_{index + 1:03d}.pdf")

    try:
        merger = InvoiceMerger()
        infos = merger.analyze(saved_paths)

        return {
            "task_id": task_id,
            "invoices": [
                {
                    "filename": original_names[idx] if idx < len(original_names) else info.filename,
                    "width": round(info.original_width, 1),
                    "height": round(info.original_height, 1),
                    "page_count": info.page_count,
                }
                for idx, info in enumerate(infos)
            ],
            "total_count": len(infos),
        }
    except Exception as e:
        logger.error(f"Invoice analyze task {task_id} failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/merge", response_model=TaskResponse, summary="Merge invoices into A4 PDF")
async def merge_invoices(
    task_id: str = Form(...),
    per_page: int = Form(4),
    margin: str = Form("standard"),
    crop_marks: bool = Form(True),
    page_numbers: bool = Form(True),
):
    task_dir = get_task_dir(TEMP_DIR, task_id)
    if not os.path.exists(task_dir):
        raise HTTPException(status_code=404, detail="任务不存在或已过期，请重新上传")

    # Collect all PDF files in task dir
    pdf_paths = [
        safe_join(task_dir, f)
        for f in os.listdir(task_dir)
        if f.lower().endswith(".pdf")
    ]

    if not pdf_paths:
        raise HTTPException(status_code=404, detail="未找到发票文件")

    try:
        merger = InvoiceMerger()
        merger.analyze(pdf_paths)

        output_path = safe_join(task_dir, "merged_invoices.pdf")
        info = merger.merge(
            output_path=output_path,
            per_page=per_page,
            margin=margin if margin in ("narrow", "standard", "wide") else "standard",
            crop_marks=crop_marks,
            page_numbers=page_numbers,
        )

        download_url = f"/api/v1/invoice/download/{task_id}"

        logger.info(
            f"Invoice merge task {task_id} completed: {info['invoices_count']} -> {info['page_count']} page(s)"
        )
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message=f"已合并 {info['invoices_count']} 张发票为 {info['page_count']} 页 A4 PDF",
            download_url=download_url,
            file_count=1,
        )
    except Exception as e:
        logger.error(f"Invoice merge task {task_id} failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/download/{task_id}", summary="Download merged invoice PDF")
async def download_merged(task_id: str, preview: bool = False):
    task_dir = get_task_dir(TEMP_DIR, task_id)
    if not os.path.exists(task_dir):
        raise HTTPException(status_code=404, detail="任务不存在或已过期")

    output_path = safe_join(task_dir, "merged_invoices.pdf")
    if not os.path.exists(output_path):
        raise HTTPException(status_code=404, detail="输出文件不存在")

    # preview=true 时不设置 attachment，让浏览器直接预览 PDF
    if preview:
        return FileResponse(
            output_path,
            media_type="application/pdf",
        )

    return FileResponse(
        output_path,
        media_type="application/pdf",
        filename="发票合并打印.pdf",
    )
