"""发票合并共享层：OFD 转换 + PDF 归一化 + 并发锁。

阶段①统一化重构（docs/INVOICE_MERGE_UNIFICATION_PLAN.md §5.1）：
- 把 ofd_invoice.py 的 _normalize_pdf / _convert_and_normalize / _cleanup_intermediate
  / 全局锁提取为进程级单例，统一端点（invoice.py）与旧端点（ofd_invoice.py）共用。
- 对外仅两个入口：
  * convert_ofd_batch(task_dir, ofd_paths, original_names) —— fail-fast 整批转换
  * OFD_INVOICE_LOCK —— 进程级锁（限并发=1，防 2GB 服务器 OOM）
- 归一化后处理（原设计 §2.2 步骤 3.5）：easyofd 转出 PDF MediaBox 放大 25/9，
  归一到 ~595pt 宽（矢量保真），渲染内存峰值 ~95MB/页 → ~12MB/页。
"""
import os
import threading
from typing import List

from fastapi import HTTPException

from app.services.ofd_converter import OFDConverter
from app.services.ofd_validator import (  # noqa: F401  normalize_pdf 2026-09-08 上移至 ofd_validator，此处保留导出兼容
    OfdEncryptedError,
    OfdFileError,
    normalize_pdf,
    validate_ofd_zip,
)
from app.core.logger import get_logger

logger = get_logger(__name__)

# 进程级锁：限并发=1，防 2GB 服务器 OOM（转换+归一化+排版为 CPU/内存密集）
OFD_INVOICE_LOCK = threading.Lock()


def convert_ofd_batch(
    task_dir: str,
    ofd_paths: List[str],
    original_names: List[str],
    start_index: int = 1,
) -> List[str]:
    """逐个 OFD → PDF → 归一化（fail-fast）。

    任一转换失败 → 整批失败（发票合并不允许部分成功，原设计评审 P4），
    并清理已生成的中间文件避免脏数据残留。
    返回转换后的 .pdf 路径列表（invoice_NNN.pdf）。

    start_index：产物序号基准。独立批次（旧 ofd-invoice 端点）传 1（默认），
    混合批次（统一 invoice 端点）传该 OFD 在整批上传里的全局序号，
    保证与 PDF 直存的 invoice_NNN.pdf 命名空间对齐、序号连续不冲突。
    """
    converter = OFDConverter()
    pdf_paths: List[str] = []
    for i, ofd_path in enumerate(ofd_paths):
        idx = start_index + i
        raw_pdf = os.path.join(task_dir, f"invoice_{idx:03d}_raw.pdf")
        # zip 预扫描（对抗审查加固）：与 /ofd/view 同一校验，拦截 zip bomb
        # （含伪造中央目录变体）与加密文件——本函数是统一/旧两个发票合并端点
        # 的 OFD 唯一转换入口，单点覆盖。
        name = original_names[i] if i < len(original_names) else f"第 {idx} 个文件"
        try:
            validate_ofd_zip(ofd_path)
        except OfdEncryptedError:
            cleanup_intermediate(task_dir)
            raise HTTPException(
                status_code=400,
                detail=f"第 {idx} 个文件「{name}」已加密，请先解密后重试。",
            )
        except OfdFileError as e:
            cleanup_intermediate(task_dir)
            logger.info(f"OFD 预扫描拦截（第 {idx} 个文件 {name}）: {e}")
            raise HTTPException(
                status_code=400,
                detail=f"第 {idx} 个文件「{name}」不是有效的 OFD 文件，请确认是税务系统开具的 OFD 版式发票。",
            )
        ok, msg = converter.ofd_to_pdf(ofd_path, raw_pdf)
        if not ok:
            logger.error(f"OFD 转换失败（第 {idx} 个文件 {name}）: {msg}")
            cleanup_intermediate(task_dir)
            raise HTTPException(
                status_code=400,
                detail=f"第 {idx} 个文件「{name}」无法解析，请确认是税务系统开具的 OFD 版式发票。",
            )
        final_pdf = os.path.join(task_dir, f"invoice_{idx:03d}.pdf")
        normalize_pdf(raw_pdf, final_pdf)
        try:
            if os.path.exists(raw_pdf):
                os.remove(raw_pdf)
        except OSError:
            pass
        pdf_paths.append(final_pdf)
    return pdf_paths


def cleanup_intermediate(task_dir: str) -> None:
    """清理转换中间产物与半成品 invoice PDF（fail-fast 场景）。"""
    for fn in os.listdir(task_dir):
        if fn.endswith("_raw.pdf") or (fn.startswith("invoice_") and fn.endswith(".pdf")):
            try:
                os.remove(os.path.join(task_dir, fn))
            except OSError:
                pass
