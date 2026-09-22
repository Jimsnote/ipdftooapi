import os
from typing import List
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse

from app.models.schemas import TaskResponse
from app.services.invoice_merger import InvoiceMerger
from app.services.invoice_merge_shared import OFD_INVOICE_LOCK, convert_ofd_batch
from app.core.logger import get_logger
from app.core.file_security import get_task_dir, make_task_dir, safe_join, save_upload_file

router = APIRouter()
logger = get_logger(__name__)

TEMP_DIR = os.path.abspath("/app/temp" if os.path.exists("/app/temp") else "./temp")
os.makedirs(TEMP_DIR, exist_ok=True)

MAX_INVOICE_FILES = 50
MAX_INVOICE_SIZE = 10 * 1024 * 1024  # 10MB per file
PDF_EXTENSIONS = (".pdf",)
OFD_EXTENSIONS = (".ofd",)


def raise_processing_error(error: Exception):
    if isinstance(error, HTTPException):
        raise error
    if isinstance(error, ValueError):
        raise HTTPException(status_code=400, detail=str(error))
    # 未知异常不向客户端泄露内部细节，完整信息仅入日志
    logger.error(f"Invoice processing error: {error!r}")
    raise HTTPException(status_code=500, detail="服务器处理失败，请稍后重试")


@router.post("/analyze", summary="Analyze uploaded invoices (PDF/OFD mixed) and return dimensions")
async def analyze_invoices(
    files: List[UploadFile] = File(...),
):
    """混合上传：.ofd 后缀走锁内 OFD→PDF 转换+归一化，其余按 PDF 直存。

    统一化方案 §5.1：分流逻辑从 ofd_invoice.py 挪进上传循环，
    merge 阶段消费的本来就是 invoice_NNN.pdf，零改动。
    """
    if len(files) > MAX_INVOICE_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"最多上传 {MAX_INVOICE_FILES} 张发票",
        )

    task_id, task_dir = make_task_dir(TEMP_DIR)

    saved_paths = []
    original_names = []
    ofd_paths = []  # (index_in_saved, path) 待转换的 OFD
    for index, f in enumerate(files):
        name = (f.filename or "").lower()
        if name.endswith(OFD_EXTENSIONS):
            path = save_upload_file(
                f,
                safe_join(task_dir, f"input_{index + 1:03d}.ofd"),
                MAX_INVOICE_SIZE,
                OFD_EXTENSIONS,
            )
            ofd_paths.append((index, path))
            saved_paths.append(None)  # 占位，转换后回填
        else:
            path = save_upload_file(
                f,
                safe_join(task_dir, f"invoice_{index + 1:03d}.pdf"),
                MAX_INVOICE_SIZE,
                PDF_EXTENSIONS,
            )
            saved_paths.append(path)
        original_names.append(f.filename or f"invoice_{index + 1:03d}.pdf")

    # OFD 批量转换（fail-fast；锁内防 2GB 服务器 OOM）。
    # start_index 用第一个 OFD 的全局序号，产物直接命名为 invoice_NNN.pdf
    #（NNN = 上传顺序号），与 PDF 直存命名空间连续对齐。
    if ofd_paths:
        try:
            with OFD_INVOICE_LOCK:
                convert_ofd_batch(
                    task_dir,
                    [p for _, p in ofd_paths],
                    [original_names[i] for i, _ in ofd_paths],
                    start_index=ofd_paths[0][0] + 1,
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Invoice analyze (OFD convert) task {task_id} failed: {e}")
            raise_processing_error(e)
        for idx, _ in ofd_paths:
            saved_paths[idx] = safe_join(task_dir, f"invoice_{idx + 1:03d}.pdf")

    pdf_paths = [p for p in saved_paths if p]

    try:
        merger = InvoiceMerger()
        infos = merger.analyze(pdf_paths)

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
        raise_processing_error(e)


@router.post("/merge", response_model=TaskResponse, summary="Merge invoices into A4 PDF")
async def merge_invoices(
    task_id: str = Form(...),
    per_page: int = Form(4),
    margin: str = Form("standard"),
    crop_marks: bool = Form(True),
    page_numbers: bool = Form(True),
    layout: str = Form("stacked"),
    binding_mm: float = Form(0),
):
    task_dir = get_task_dir(TEMP_DIR, task_id)
    if not os.path.exists(task_dir):
        raise HTTPException(status_code=404, detail="任务不存在或已过期，请重新上传")

    # Collect all PDF files in task dir（invoice_NNN.pdf，含 OFD 转换产物）
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
            layout=layout if layout in ("stacked", "paste_sheet") else "stacked",
            binding_mm=max(0.0, min(binding_mm, 80.0)),
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
        raise_processing_error(e)


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
