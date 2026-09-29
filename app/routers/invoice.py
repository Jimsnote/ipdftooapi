import json
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

# 审计 #4：per_page 白名单（service 层兜底之前路由先拦截，非法值一律 400）
VALID_PER_PAGE = {1, 2, 4, 6, 9}


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
    # 产物直接命名为 invoice_NNN.pdf（NNN = 该 OFD 的全局上传序号+1），
    # 与 PDF 直存命名空间对齐——审计 #1 修复：连续编号在混合序列下会覆盖直存 PDF。
    if ofd_paths:
        try:
            with OFD_INVOICE_LOCK:
                convert_ofd_batch(
                    task_dir,
                    ofd_paths,
                    [original_names[i] for i, _ in ofd_paths],
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Invoice analyze (OFD convert) task {task_id} failed: {e}")
            raise_processing_error(e)
        for idx, _ in ofd_paths:
            saved_paths[idx] = safe_join(task_dir, f"invoice_{idx + 1:03d}.pdf")

    pdf_paths = [p for p in saved_paths if p]

    # 审计 #1 加固：任一产物缺失立即失败，绝不让 analyze 阶段 FileNotFoundError
    # 变成 500（静默覆盖/漏转换在此处兜底暴露）
    missing = [p for p in pdf_paths if not os.path.exists(p)]
    if missing:
        logger.error(f"Invoice analyze task {task_id}: missing converted files: {missing}")
        raise HTTPException(
            status_code=500,
            detail="发票转换结果异常，请重新上传后再试",
        )

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
    order: str = Form(""),
):
    task_dir = get_task_dir(TEMP_DIR, task_id)
    if not os.path.exists(task_dir):
        raise HTTPException(status_code=404, detail="任务不存在或已过期，请重新上传")

    if per_page not in VALID_PER_PAGE:
        raise HTTPException(
            status_code=400,
            detail="每页张数仅支持 1、2、4、6、9",
        )

    # 收集任务目录内的发票 PDF（invoice_NNN.pdf；input_*.ofd 是转换源，不参与合并）。
    # sorted() 保证缺省顺序 = 上传顺序（os.listdir 在 ext4 上不保证顺序，大批次可能乱序）
    invoice_files = sorted(
        f for f in os.listdir(task_dir)
        if f.startswith("invoice_") and f.lower().endswith(".pdf")
    )
    if not invoice_files:
        raise HTTPException(status_code=404, detail="未找到发票文件")

    pdf_paths: List[str]
    if order.strip():
        # 前端传入用户最终确认的发票顺序：JSON 数组，元素为 0-based 原始序号
        # （对应 analyze 的上传顺序，文件名即 invoice_{序号+1:03d}.pdf）。
        # 用户删除/排序只改前端列表，必须由 order 显式告知，否则产物与预览不一致。
        try:
            idx_list = json.loads(order)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="发票顺序参数格式错误，请重新上传")
        if (
            not isinstance(idx_list, list)
            or not idx_list
            or not all(isinstance(i, int) and not isinstance(i, bool) for i in idx_list)
            or len(set(idx_list)) != len(idx_list)
            or not set(idx_list) <= set(range(len(invoice_files)))
        ):
            # 允许 order 为 [0..N-1] 的任意无重复子集：覆盖"排序"（全量排列）与
            # "删除"（子集，被删发票的索引不出现）两种场景
            raise HTTPException(
                status_code=400,
                detail="发票顺序与任务内容不一致，请重新上传发票后再合并",
            )
        pdf_paths = [safe_join(task_dir, f"invoice_{i + 1:03d}.pdf") for i in idx_list]
    else:
        pdf_paths = [safe_join(task_dir, f) for f in invoice_files]

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
