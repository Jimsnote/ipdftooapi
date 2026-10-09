"""发票合并共享层：OFD 转换 + PDF 归一化 + 并发锁。

阶段①统一化重构（docs/INVOICE_MERGE_UNIFICATION_PLAN.md §5.1）：
- 把 ofd_invoice.py 的 _normalize_pdf / _convert_and_normalize / _cleanup_intermediate
  / 全局锁提取为进程级单例，统一端点（invoice.py）与旧端点（ofd_invoice.py）共用。
- 对外仅两个入口：
  * convert_ofd_batch(task_dir, ofd_items, original_names) —— fail-fast 整批转换
    （ofd_items = [(全局 0 基序号, 路径)]，产物按全局序号编号防覆盖，审计 #1）
  * OFD_INVOICE_LOCK —— 进程级锁（限并发=1，防 2GB 服务器 OOM）
- 归一化后处理（原设计 §2.2 步骤 3.5）：easyofd 转出 PDF MediaBox 放大 25/9，
  归一到 ~595pt 宽（矢量保真），渲染内存峰值 ~95MB/页 → ~12MB/页。
"""
import os
import threading
from typing import List, Tuple

from fastapi import HTTPException

from app.services.ofd_converter import OFDConverter
from app.services.ofd_validator import (  # noqa: F401  normalize_pdf 2026-09-08 上移至 ofd_validator，此处保留导出兼容
    OfdEncryptedError,
    OfdFileError,
    convert_ofd_to_pdf,
    normalize_pdf,
    validate_ofd_zip,
)
from app.core.logger import get_logger

logger = get_logger(__name__)

# 进程级锁：限并发=1，防 2GB 服务器 OOM（转换+归一化+排版为 CPU/内存密集）
OFD_INVOICE_LOCK = threading.Lock()


def convert_ofd_batch(
    task_dir: str,
    ofd_items: List[Tuple[int, str]],
    original_names: List[str],
) -> List[str]:
    """逐个 OFD → PDF → 归一化（fail-fast）。

    任一转换失败 → 整批失败（发票合并不允许部分成功，原设计评审 P4），
    并清理已生成的中间文件避免脏数据残留。
    返回转换后的 .pdf 路径列表（invoice_NNN.pdf）。

    ofd_items：[(全局 0 基序号, OFD 路径)]。产物命名 invoice_{序号+1:03d}.pdf
    直接沿用该 OFD 在整批上传里的全局序号（审计 #1 修复：旧版用
    start_index + 子列表下标连续编号，混合序列 [OFD, PDF, OFD] 时第二个
    OFD 产物会覆盖 PDF 直存的 invoice_002.pdf——必须按全局序号编号）。
    """
    converter = OFDConverter()
    pdf_paths: List[str] = []
    for item_index, (global_idx, ofd_path) in enumerate(ofd_items):
        idx = global_idx + 1
        raw_pdf = os.path.join(task_dir, f"invoice_{idx:03d}_raw.pdf")
        # zip 预扫描（对抗审查加固）：与 /ofd/view 同一校验，拦截 zip bomb
        # （含伪造中央目录变体）与加密文件——本函数是统一/旧两个发票合并端点
        # 的 OFD 唯一转换入口，单点覆盖。
        name = original_names[item_index] if item_index < len(original_names) else f"第 {idx} 个文件"
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


async def convert_ofd_batch_async(
    task_dir: str,
    ofd_items: List[Tuple[int, str]],
    original_names: List[str],
) -> None:
    """async 版批量转换（2026-10-09 对抗审查 P1-6）：单文件转换走进程池。

    与同步版 convert_ofd_batch 的差异：
      - 转换经 ofd_validator.convert_ofd_to_pdf 执行——复用 /ofd-to-pdf 的
        全部防护设施：进程池隔离（easyofd 硬崩溃只死 worker）、60s 超时、
        超时真取消、Semaphore 全局并发限制（2026-10-09 起 = gunicorn
        workers 数）。
      - 旧实现（同步版）在 API worker 进程内直接跑 easyofd：损坏文件可
        长时间占住 OFD_INVOICE_LOCK（最坏永久），无超时、无进程隔离。
      - 并发控制不再依赖进程内线程锁（跨 gunicorn worker 无效），交给
        convert_ofd_to_pdf 内部的信号量。
      - normalize 由 _convert_job 内联完成（crop=True，与旧版语义一致），
        产物直接落 invoice_NNN.pdf。

    :param ofd_items: [(全局 0 基序号, OFD 路径)]，与同步版约定一致
    :raises HTTPException: 预扫描拦截 400 / 转换失败或超时 400（fail-fast，
              整批失败并清理中间产物）
    """
    for item_index, (global_idx, ofd_path) in enumerate(ofd_items):
        idx = global_idx + 1
        name = (
            original_names[item_index]
            if item_index < len(original_names)
            else f"第 {idx} 个文件"
        )
        # zip 预扫描：与同步版同一校验（zip bomb / 加密 / 伪造中央目录）
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

        final_pdf = os.path.join(task_dir, f"invoice_{idx:03d}.pdf")
        ok, msg = await convert_ofd_to_pdf(ofd_path, final_pdf, crop=True)
        if not ok:
            logger.error(f"OFD 转换失败（第 {idx} 个文件 {name}）: {msg}")
            cleanup_intermediate(task_dir)
            raise HTTPException(
                status_code=400,
                detail=f"第 {idx} 个文件「{name}」无法解析，请确认是税务系统开具的 OFD 版式发票。",
            )
