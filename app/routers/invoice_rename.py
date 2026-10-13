"""发票批量重命名路由（二期工具三）。

- POST /invoice/rename-batch              混传数电票 XML/OFD/PDF ≤50 →
  按票面信息批量重命名 → 打包 ZIP（含 _重命名报告.txt）
- GET  /invoice/rename-download/{task_id} 下载重命名产物 ZIP

设计依据：docs/2026-09-30_100232-invoice-batch-rename-design.md §5/§6
2026-10-13 §6.4 函数提取（P0 / D2 拍板）：编排体逐行搬移至
services/invoice_rename.py::run_batch（行为不变，418 基线护航），
本路由变一行委托——网站入口与 skill 付费网关（skill_gateway）共用同一编排。
"""
import os

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.core.file_security import get_task_dir, safe_join
from app.models.invoice_extract import RenameBatchResponse
from app.services.invoice_rename import run_batch

router = APIRouter()

TEMP_DIR = os.path.abspath("/app/temp" if os.path.exists("/app/temp") else "./temp")


@router.post("/rename-batch", response_model=RenameBatchResponse, summary="按票面信息批量重命名发票文件")
def rename_batch(
    files: list[UploadFile] = File(...),
    prefix: str = Form(""),
    include_amount: bool = Form(False),
    include_type: bool = Form(False),
):
    return run_batch(files, prefix, include_amount, include_type)


@router.get("/rename-download/{task_id}", summary="下载发票批量重命名 ZIP")
def rename_download(task_id: str):
    task_dir = get_task_dir(TEMP_DIR, task_id)
    if not os.path.exists(task_dir):
        raise HTTPException(status_code=404, detail="任务不存在或已过期")

    entries = [f for f in os.listdir(task_dir) if f.startswith("invoice_renamed_")]
    if not entries:
        raise HTTPException(status_code=404, detail="重命名产物不存在")
    path = safe_join(task_dir, entries[0])
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"invoice_renamed_{task_id[:8]}.zip",
    )
