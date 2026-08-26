"""发票信息批量提取路由（二期工具二）。

- POST /invoice/extract-analyze   混传 XML/OFD/PDF ≤50 个 → records + failures
  * 逐文件失败隔离：类型不支持/解析失败都进 failures，不阻断整批
  * analyze 阶段全程内存操作，原始文件不写磁盘（隐私强化）
  * 同号去重提示：同一发票号码出现多次时向相关行追加 warning
- POST /invoice/extract-export    前端表格当前态 → csv/xlsx 台账文件
- GET  /invoice/extract-download/{task_id}   下载台账文件

设计依据：docs/INVOICE_EXTRACT_PHASE2_PLAN.md（§2 决策 D3/D4、§3 接口）
"""
import os
from typing import List

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.core.logger import get_logger
from app.core.file_security import get_task_dir, make_task_dir, safe_join
from app.models.invoice_extract import (
    AnalyzeResponse,
    ExportRequest,
    ExportResponse,
    ExtractFailureItem,
    InvoiceRecord,
)
from app.services.invoice_extract import (
    InvoiceExtractError,
    export_csv,
    export_xlsx,
    extract_bytes,
    build_filename,
)
from app.services.invoice_extract.extractor import SUPPORTED_EXTENSIONS

router = APIRouter()
logger = get_logger(__name__)

TEMP_DIR = os.path.abspath("/app/temp" if os.path.exists("/app/temp") else "./temp")
os.makedirs(TEMP_DIR, exist_ok=True)

MAX_FILES = 50
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB / 单文件，与发票合并对齐
MAX_EXPORT_ROWS = 10000

_CSV_MEDIA = "text/csv; charset=utf-8"
_XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _sniff_and_check(file: UploadFile) -> str:
    """返回小写扩展名；类型/大小不合法抛 InvoiceExtractError（进 failures）。"""
    name = file.filename or ""
    ext = None
    for candidate in SUPPORTED_EXTENSIONS:
        if name.lower().endswith(candidate):
            ext = candidate
            break
    if ext is None:
        raise InvoiceExtractError("不支持的文件类型（仅支持 .xml / .ofd / .pdf）")
    if file.size and file.size > MAX_FILE_SIZE:
        raise InvoiceExtractError(f"文件超过 {MAX_FILE_SIZE // (1024 * 1024)}MB 上限")
    return ext


def _read_checked(file: UploadFile, ext: str) -> bytes:
    data = file.file.read(MAX_FILE_SIZE + 1)
    if len(data) > MAX_FILE_SIZE:
        raise InvoiceExtractError(f"文件超过 {MAX_FILE_SIZE // (1024 * 1024)}MB 上限")
    if not data:
        raise InvoiceExtractError("文件为空")
    if ext == ".xml":
        probe = data.lstrip(b"\xef\xbb\xbf \t\r\n")
        if not probe.startswith(b"<"):
            raise InvoiceExtractError("内容不是 XML 文本")
    elif ext == ".pdf":
        if not data.startswith(b"%PDF-"):
            raise InvoiceExtractError("PDF 文件头校验失败")
    elif ext == ".ofd":
        if not data.startswith(b"PK"):
            raise InvoiceExtractError("OFD 文件头校验失败（应为 ZIP 包）")
    return data


@router.post("/extract-analyze", response_model=AnalyzeResponse, summary="批量提取发票关键字段")
def extract_analyze(files: List[UploadFile] = File(...)):
    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一个文件")
    if len(files) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"一次最多上传 {MAX_FILES} 个文件")

    records: List[InvoiceRecord] = []
    failures: List[ExtractFailureItem] = []

    for index, f in enumerate(files):
        name = f.filename or f"file_{index + 1}"
        try:
            ext = _sniff_and_check(f)
            data = _read_checked(f, ext)
            rec = extract_bytes(data, name, ext)
            records.append(rec)
        except InvoiceExtractError as e:
            failures.append(ExtractFailureItem(file=name, reason=str(e)))
        except Exception as e:  # noqa: BLE001
            logger.error(f"extract failed for {name}: {e}")
            failures.append(ExtractFailureItem(file=name, reason="解析失败，请确认文件未损坏"))

    # 同号提示（不去重，是否保留由用户在表格中处理）
    seen: dict = {}
    for rec in records:
        if rec.invoice_number:
            seen.setdefault(rec.invoice_number, []).append(rec)
    for number, group in seen.items():
        if len(group) > 1:
            msg = f"发票号码 {number} 出现 {len(group)} 次，请核对是否重复上传"
            for rec in group:
                rec.warnings.append(msg)

    return AnalyzeResponse(
        records=records,
        failures=failures,
        success_count=len(records),
        failure_count=len(failures),
    )


@router.post("/extract-export", response_model=ExportResponse, summary="导出台账 CSV/Excel")
def extract_export(payload: ExportRequest):
    fmt = (payload.format or "xlsx").lower()
    if fmt not in ("csv", "xlsx"):
        raise HTTPException(status_code=400, detail="format 仅支持 csv 或 xlsx")
    if not payload.rows:
        raise HTTPException(status_code=400, detail="没有可导出的数据行")
    if len(payload.rows) > MAX_EXPORT_ROWS:
        raise HTTPException(status_code=400, detail=f"单次最多导出 {MAX_EXPORT_ROWS} 行")

    if fmt == "csv":
        content = export_csv(payload.rows)
        media_type = _CSV_MEDIA
    else:
        content = export_xlsx(payload.rows)
        media_type = _XLSX_MEDIA

    filename = build_filename(fmt)
    task_id, task_dir = make_task_dir(TEMP_DIR)
    out_path = safe_join(task_dir, filename)
    with open(out_path, "wb") as fh:
        fh.write(content)

    logger.info(f"invoice extract export task {task_id}: {len(payload.rows)} rows -> {fmt}")
    return ExportResponse(
        download_url=f"/api/v1/invoice/extract-download/{task_id}",
        filename=filename,
        count=len(payload.rows),
    )


@router.get("/extract-download/{task_id}", summary="下载发票台账文件")
def extract_download(task_id: str):
    task_dir = get_task_dir(TEMP_DIR, task_id)
    if not os.path.exists(task_dir):
        raise HTTPException(status_code=404, detail="任务不存在或已过期")

    entries = [f for f in os.listdir(task_dir) if f.startswith("发票台账_")]
    if not entries:
        raise HTTPException(status_code=404, detail="台账文件不存在")
    path = safe_join(task_dir, entries[0])
    media = _CSV_MEDIA if entries[0].endswith(".csv") else _XLSX_MEDIA
    return FileResponse(path, media_type=media, filename=entries[0])
