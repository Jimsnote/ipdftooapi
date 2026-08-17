"""OFD 发票合并路由：上传 OFD → 逐个转 PDF → 归一化 → 复用 InvoiceMerger 排版。

设计依据：docs/OFD_INVOICE_MERGE_DESIGN.md
- 复用 InvoiceMerger（排版逻辑零改动），仅新增 OFD 校验 + 转换前处理。
- 归一化后处理（§2.2 步骤 3.5 / 评审 P1）：easyofd 转出的 PDF MediaBox 被放大 25/9，
  导致 300DPI 渲染内存峰值 ~95MB/页。归一化到 ~595pt 宽（矢量、保比例）后压回 ~12MB/页。
- 转换失败 fail-fast 整批 400（评审 P4）：发票合并不允许部分成功。
- 端点用 def + 全局锁（照抄 OCR 路由）：限并发=1，防止 2GB 服务器 OOM，且不阻塞 event loop。
"""
import os
import shutil
import threading
from typing import List

import fitz
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse

from app.models.schemas import TaskResponse
from app.services.invoice_merger import InvoiceMerger
from app.services.ofd_converter import OFDConverter
from app.core.logger import get_logger
from app.core.file_security import get_task_dir, make_task_dir, safe_join, save_upload_file

router = APIRouter()
logger = get_logger(__name__)

TEMP_DIR = os.path.abspath("/app/temp" if os.path.exists("/app/temp") else "./temp")
os.makedirs(TEMP_DIR, exist_ok=True)

MAX_OFD_FILES = 50
MAX_OFD_SIZE = 10 * 1024 * 1024  # 10MB / 单张，与 invoice-merge 对齐（评审 P2/P5）
OFD_EXTENSIONS = (".ofd",)

# 全局锁：限并发=1，防 2GB 服务器 OOM（转换+归一化+排版为 CPU/内存密集，照抄 OCR）
_ofd_invoice_lock = threading.Lock()


def _normalize_pdf(src: str, dst: str, threshold_pt: float = 800.0) -> str:
    """把 easyofd 转出的超大 PDF 等比归一到正常物理尺寸（矢量保真）。

    消除 MediaBox 放大 25/9 带来的渲染内存峰值（~95MB/页 → ~12MB/页），
    并顺带修复绝对物理尺寸失真。宽高同比例缩放，A4 排版不会拉伸。
    任何异常都 fallback 为复制原文件，绝不让整批任务因归一化失败而崩。
    """
    doc = fitz.open(src)
    try:
        if doc[0].rect.width <= threshold_pt:
            # 尺寸已正常，直接复制
            doc.close()
            shutil.copyfile(src, dst)
            return dst
        k = 595.0 / doc[0].rect.width  # 目标宽 ~A4 宽，保持宽高比
        out = fitz.open()
        for p in doc:
            np_ = out.new_page(width=p.rect.width * k, height=p.rect.height * k)
            np_.show_pdf_page(np_.rect, doc, p.number)
        doc.close()
        out.save(dst)
        out.close()
        return dst
    except Exception as e:
        logger.warning(f"_normalize_pdf failed, fallback copy: {e}")
        try:
            doc.close()
        except Exception:
            pass
        shutil.copyfile(src, dst)
        return dst


def _convert_and_normalize(task_dir: str, saved_ofd_paths: List[str], original_names: List[str]) -> List[str]:
    """逐个 OFD → PDF → 归一化。

    任一转换失败 → 整批失败（fail-fast，发票合并不允许部分成功，评审 P4）。
    返回转换后的 .pdf 路径列表（invoice_NNN.pdf）。
    """
    converter = OFDConverter()
    pdf_paths: List[str] = []
    for idx, ofd_path in enumerate(saved_ofd_paths):
        raw_pdf = safe_join(task_dir, f"invoice_{idx + 1:03d}_raw.pdf")
        ok, msg = converter.ofd_to_pdf(ofd_path, raw_pdf)
        if not ok:
            name = original_names[idx] if idx < len(original_names) else f"第 {idx + 1} 个文件"
            # 清理已生成的中间文件，避免脏数据残留
            _cleanup_intermediate(task_dir)
            raise HTTPException(
                status_code=400,
                detail=f"第 {idx + 1} 个文件「{name}」无法解析，请确认是税务系统开具的 OFD 版式发票。",
            )
        final_pdf = safe_join(task_dir, f"invoice_{idx + 1:03d}.pdf")
        _normalize_pdf(raw_pdf, final_pdf)
        try:
            if os.path.exists(raw_pdf):
                os.remove(raw_pdf)
        except OSError:
            pass
        pdf_paths.append(final_pdf)
    return pdf_paths


def _cleanup_intermediate(task_dir: str) -> None:
    for fn in os.listdir(task_dir):
        if fn.endswith("_raw.pdf") or fn.startswith("invoice_") and fn.endswith(".pdf"):
            try:
                os.remove(safe_join(task_dir, fn))
            except OSError:
                pass


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

    with _ofd_invoice_lock:
        try:
            pdf_paths = _convert_and_normalize(task_dir, saved_paths, original_names)
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
            raise HTTPException(status_code=500, detail=f"OFD 发票处理失败：{e}")


@router.post("/merge", response_model=TaskResponse, summary="合并 OFD 发票为 A4 PDF")
def merge_ofd_invoices(
    task_id: str = Form(...),
    per_page: int = Form(4),
    margin: str = Form("standard"),
    crop_marks: bool = Form(True),
    page_numbers: bool = Form(True),
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

    with _ofd_invoice_lock:
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
            raise HTTPException(status_code=500, detail=f"OFD 发票合并失败：{e}")


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
