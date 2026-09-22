"""OFD 发票合并路由（旧端点，统一化过渡期保留 3 个月）。

设计依据：docs/OFD_INVOICE_MERGE_DESIGN.md（首版）
          docs/INVOICE_MERGE_UNIFICATION_PLAN.md（统一化，§5.1/§六）
- 排版与 OFD 转换逻辑已提取到 services/invoice_merge_shared.py，
  本模块仅保留路由壳，内部调用共享函数——对外 URL/行为不变。
- 统一端点 /api/v1/invoice/analyze 已支持混合上传；本端点仅供旧前端与
  外部直接调用方过渡，P3 观察期后（约 2026-12）视调用量下线。
"""
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

MAX_OFD_FILES = 50
MAX_OFD_SIZE = 10 * 1024 * 1024  # 10MB / 单张，与 invoice-merge 对齐（评审 P2/P5）
OFD_EXTENSIONS = (".ofd",)


@router.post("/analyze", summary="上传 OFD 发票并转换为 PDF，返回尺寸信息")
def analyze_ofd_invoices(files: List[UploadFile] = File(...)):
    if len(files) > MAX_OFD_FILES:
        raise HTTPException(status_code=400, detail=f"最多上传 {MAX_OFD_FILES} 张 OFD 发票")

    task_id, task_dir = make_task_dir(TEMP_DIR)

    saved_paths: List[str] = []
    original_names: List[str] = []
    for index, f in enumerate(files):
        path = save_upload_file(
            f,
            safe_join(task_dir, f"invoice_{index + 1:03d}.ofd"),
            MAX_OFD_SIZE,
            OFD_EXTENSIONS,
        )
        saved_paths.append(path)
        original_names.append(f.filename or f"invoice_{index + 1:03d}.ofd")

    with OFD_INVOICE_LOCK:
        try:
            pdf_paths = convert_ofd_batch(task_dir, saved_paths, original_names)
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
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"OFD invoice analyze task {task_id} failed: {e}")
            raise HTTPException(status_code=500, detail="OFD 发票处理失败，请重试或更换文件")


@router.post("/merge", response_model=TaskResponse, summary="合并 OFD 发票为 A4 PDF")
def merge_ofd_invoices(
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

    # merge 收集 task 目录下所有 .pdf（与 invoice 路由一致）
    pdf_paths = [
        safe_join(task_dir, f)
        for f in os.listdir(task_dir)
        if f.lower().endswith(".pdf")
    ]
    if not pdf_paths:
        raise HTTPException(status_code=404, detail="未找到发票文件，请重新上传")

    with OFD_INVOICE_LOCK:
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

            download_url = f"/api/v1/ofd-invoice/download/{task_id}"

            logger.info(
                f"OFD invoice merge task {task_id} completed: {info['invoices_count']} -> {info['page_count']} page(s)"
            )
            return TaskResponse(
                task_id=task_id,
                status="completed",
                message=f"已合并 {info['invoices_count']} 张 OFD 发票为 {info['page_count']} 页 A4 PDF",
                download_url=download_url,
                file_count=1,
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"OFD invoice merge task {task_id} failed: {e}")
            raise HTTPException(status_code=500, detail="OFD 发票合并失败，请重试")


@router.get("/download/{task_id}", summary="下载合并后的 OFD 发票 A4 PDF")
def download_merged(task_id: str, preview: bool = False):
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
        filename="OFD发票合并打印.pdf",
    )
