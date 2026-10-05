"""发票批量重命名路由（二期工具三）。

- POST /invoice/rename-batch              混传数电票 XML/OFD/PDF ≤50 →
  按票面信息批量重命名 → 打包 ZIP（含 _重命名报告.txt）
- GET  /invoice/rename-download/{task_id} 下载重命名产物 ZIP

设计依据：docs/2026-09-30_100232-invoice-batch-rename-design.md §5/§6
- 提取复用 invoice_extract 包（extract_bytes 字节流入口），零解析代码复制；
- 未识别文件不 fail 整批：保留原名进 ZIP + skipped 清单（§4.4）；
- OFD 过 validate_ofd_zip zip 炸弹/加密预扫描（§5.3）；
- 下载独立于 /invoice/download（该端点只认发票合并产物，避免语义混淆）；
- 全部状态保存在请求局部变量内（无模块级可变状态，并发安全）。
"""
import os
from typing import List, Tuple

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.core.file_security import get_task_dir, make_task_dir, safe_join
from app.core.logger import get_logger
from app.models.invoice_extract import (
    RenameBatchResponse,
    RenameSkippedItem,
)
from app.services.invoice_extract import InvoiceExtractError, extract_bytes
from app.services.invoice_extract.extractor import SUPPORTED_EXTENSIONS
from app.services.invoice_rename import (
    MAX_FILENAME_LEN,
    MAX_PREFIX_LEN,
    _sanitize_component,
    build_rename_zip,
    build_target_name,
    dedup_names,
)
from app.services.ofd_validator import (
    OFD_MSG_ENCRYPTED,
    OFD_MSG_NOT_OFD,
    OfdEncryptedError,
    OfdFileError,
    validate_ofd_zip,
)

router = APIRouter()
logger = get_logger(__name__)

TEMP_DIR = os.path.abspath("/app/temp" if os.path.exists("/app/temp") else "./temp")
os.makedirs(TEMP_DIR, exist_ok=True)

MAX_FILES = 50
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB / 单文件，与发票台账对齐
# 审查①-S1：50×10MB 全量驻留内存可达 500MB（2核2G 服务器不可承受），
# 与 merge 系 #20 守卫对齐：总量 100MB 上限，声明值预检 + 实读累计双保险
MAX_TOTAL_SIZE = 100 * 1024 * 1024


def _read_limited(file: UploadFile) -> bytes:
    """读上传内容：10MB 上限 → 413、空文件 → 400（设计 §8.2 预期）。"""
    data = file.file.read(MAX_FILE_SIZE + 1)
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"单文件不能超过 {MAX_FILE_SIZE // (1024 * 1024)}MB",
        )
    if not data:
        raise HTTPException(
            status_code=400, detail=f"文件为空：{file.filename or '未命名'}"
        )
    return data


def _sniff_ext(filename: str) -> str:
    """返回小写扩展名（含点）；白名单外抛 InvoiceExtractError（进 skipped）。"""
    lower = (filename or "").lower()
    for candidate in SUPPORTED_EXTENSIONS:
        if lower.endswith(candidate):
            return candidate
    raise InvoiceExtractError("不支持的文件类型（仅支持数电票 .xml / .ofd / .pdf）")


_MAGIC_CHECKS = {
    ".xml": lambda d: d.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"),
    ".pdf": lambda d: d.startswith(b"%PDF-"),
    ".ofd": lambda d: d.startswith(b"PK"),
}
_MAGIC_MESSAGES = {
    ".xml": "内容不是 XML 文本",
    ".pdf": "PDF 文件头校验失败",
    ".ofd": "OFD 文件头校验失败（应为 ZIP 包）",
}


def _parse_one(
    task_dir: str, index: int, orig_name: str, data: bytes, prefix: str,
    include_amount: bool, include_type: bool,
) -> Tuple[str, bytes, str]:
    """解析单文件并生成目标名。返回 (目标名, 数据, 扩展名)。

    一切业务性失败（类型/损坏/版式不支持/加密/非数电票）抛 InvoiceExtractError。
    """
    ext = _sniff_ext(orig_name)

    # 文件头校验（与台账 _read_checked 同款）：扩展名伪装的文件早失败早提示
    if not _MAGIC_CHECKS[ext](data):
        raise InvoiceExtractError(_MAGIC_MESSAGES[ext])

    # 原件落盘（仅 OFD：validate_ofd_zip 预扫描需要路径；XML/PDF 走内存字节流，
    # 审查①-S1：省去无谓落盘，任务目录磁盘占用减半）
    if ext == ".ofd":
        src_path = safe_join(task_dir, f"src_{index}{ext}")
        with open(src_path, "wb") as fh:
            fh.write(data)
        # §5.3：zip 炸弹/加密预扫描，与 /ofd/view、ofd-to-pdf 同款校验
        try:
            validate_ofd_zip(src_path)
        except OfdEncryptedError:
            raise InvoiceExtractError(OFD_MSG_ENCRYPTED)
        except OfdFileError:
            raise InvoiceExtractError(OFD_MSG_NOT_OFD)

    rec = extract_bytes(data, orig_name, ext)
    if not rec.invoice_number:
        raise InvoiceExtractError(
            "未能识别出发票号码——可能不是数电票标准版式，暂不支持"
        )
    target = build_target_name(rec, ext, prefix, include_amount, include_type)
    return target, data, ext


@router.post("/rename-batch", response_model=RenameBatchResponse, summary="按票面信息批量重命名发票文件")
def rename_batch(
    files: List[UploadFile] = File(...),
    prefix: str = Form(""),
    include_amount: bool = Form(False),
    include_type: bool = Form(False),
):
    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一个文件")
    if len(files) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"一次最多上传 {MAX_FILES} 个文件")
    if prefix and len(prefix) > MAX_PREFIX_LEN:
        raise HTTPException(status_code=400, detail=f"前缀最长 {MAX_PREFIX_LEN} 个字符")
    # 审查①-S1：声明值预检（快速失败；size 可为 None 按 0 计，后面实读累计兜底）
    if sum(f.size or 0 for f in files) > MAX_TOTAL_SIZE:
        raise HTTPException(
            status_code=400, detail="所有文件加起来不能超过 100MB，请分批处理"
        )

    task_id, task_dir = make_task_dir(TEMP_DIR)

    # 请求局部状态（并发安全）
    skipped_pairs: List[Tuple[str, str]] = []  # (原文件名, 原因)
    skipped_files: List[Tuple[bytes, str]] = []  # (数据, 原文件名) → 原名进 ZIP
    renamed: List[Tuple[str, str, bytes, str]] = []  # (原文件名, 目标名, 数据, 扩展名)
    total_read = 0  # 审查①-S1：实读字节累计（防声明值伪造）

    for index, f in enumerate(files):
        orig_name = f.filename or f"file_{index + 1}"
        data = b""
        try:
            data = _read_limited(f)
            total_read += len(data)
            if total_read > MAX_TOTAL_SIZE:
                raise HTTPException(
                    status_code=400,
                    detail="所有文件加起来不能超过 100MB，请分批处理",
                )
            target, data, ext = _parse_one(
                task_dir, index, orig_name, data, prefix, include_amount,
                include_type,
            )
            renamed.append((orig_name, target, data, ext))
        except InvoiceExtractError as e:
            # §4.4：单文件失败不 fail 整批，保留原名进 ZIP
            skipped_pairs.append((orig_name, str(e)))
            skipped_files.append((data, orig_name))
        except HTTPException:
            raise  # 413/400 等参数类错误整批返回
        except Exception as e:  # noqa: BLE001
            logger.error(f"rename parse failed for {orig_name}: {e}")
            skipped_pairs.append((orig_name, "解析失败，请确认文件未损坏"))
            skipped_files.append((data, orig_name))

    # §4.3 去重（仅重命名成功的文件；未识别文件保留原名不参与）
    targets, duplicates_resolved = dedup_names([t for _, t, _, _ in renamed])

    renamed_entries: List[Tuple[str, str]] = []
    renamed_report: List[Tuple[str, str]] = []
    for i, ((orig, target, data, ext), final) in enumerate(zip(renamed, targets)):
        final_path = safe_join(task_dir, f"out_{i}{ext}")
        with open(final_path, "wb") as fh:
            fh.write(data)
        renamed_entries.append((final_path, final))
        renamed_report.append((orig, final))

    skipped_entries: List[Tuple[str, str]] = []
    # 审查①-S3：ZIP 允许重复条目名（解压时后者覆盖前者 → 静默丢文件）。
    # skipped arcname 须对「报告名 + renamed 终名 + 先前 skipped」全量查重
    used_arcnames = {"_重命名报告.txt"} | set(targets)
    for i, (data, orig_name) in enumerate(skipped_files):
        # 原文件名也是不可信输入：进 ZIP 前必须清洗（防路径穿越/非法字符）
        arcname = _sanitize_component(orig_name, MAX_FILENAME_LEN)
        if not arcname or arcname == "_":
            arcname = f"unrecognized_{i}.bin"
        if arcname in used_arcnames:
            stem, dot, ext = arcname.rpartition(".")
            serial = 2
            candidate = f"{stem}__{serial}.{ext}" if dot else f"{arcname}__{serial}"
            while candidate in used_arcnames:
                serial += 1
                candidate = f"{stem}__{serial}.{ext}" if dot else f"{arcname}__{serial}"
            arcname = candidate
        used_arcnames.add(arcname)
        path = safe_join(task_dir, f"skip_{i}.bin")
        with open(path, "wb") as fh:
            fh.write(data)
        skipped_entries.append((path, arcname))

    zip_path = safe_join(task_dir, f"invoice_renamed_{task_id[:8]}.zip")
    build_rename_zip(
        zip_path, renamed_entries, skipped_entries, renamed_report, skipped_pairs
    )

    total = len(files)
    logger.info(
        f"invoice rename task {task_id}: total={total} renamed={len(renamed)} "
        f"skipped={len(skipped_pairs)} duplicates_resolved={duplicates_resolved}"
    )
    return RenameBatchResponse(
        task_id=task_id,
        download_url=f"/api/v1/invoice/rename-download/{task_id}",
        total=total,
        renamed=len(renamed),
        duplicates_resolved=duplicates_resolved,
        skipped=[RenameSkippedItem(file=n, reason=r) for n, r in skipped_pairs],
    )


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
