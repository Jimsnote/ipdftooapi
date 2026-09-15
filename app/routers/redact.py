"""PDF 涂黑/脱敏（真删除）路由。

API 契约见 docs/2026-09-14_231000-redact-pdf-true-redaction-design.md §3.7：
- POST /api/v1/redact        → 200 application/pdf + X-Redact-Report（base64url）
- POST /api/v1/redact/locate → 200 JSON {matches, total}（只计数不回显原文）
"""
import base64
import json
import os

from fastapi import APIRouter, File, Form, UploadFile
from fastapi.responses import FileResponse

from app.config import settings
from app.core.exceptions import FileTooLargeError, PDFProcessingError
from app.core.file_security import make_task_dir, safe_join, save_upload_file
from app.core.logger import get_logger
from app.models.schemas import RedactLocateResponse
from app.services import redactor

router = APIRouter()
logger = get_logger(__name__)

TEMP_DIR = os.path.abspath("/app/temp" if os.path.exists("/app/temp") else "./temp")
os.makedirs(TEMP_DIR, exist_ok=True)

PDF_EXTENSIONS = (".pdf",)


def _parse_json_form(raw: str, field: str, default):
    try:
        value = json.loads(raw) if raw and raw.strip() else default
    except json.JSONDecodeError:
        raise PDFProcessingError(f"{field} 参数格式无效")
    return value


def _save_redact_upload(file: UploadFile, task_dir: str) -> str:
    if file.size and file.size > settings.REDACT_MAX_SIZE:
        raise FileTooLargeError(settings.REDACT_MAX_SIZE)
    return save_upload_file(
        file,
        safe_join(task_dir, "input.pdf"),
        settings.REDACT_MAX_SIZE,
        PDF_EXTENSIONS,
    )


@router.post("", summary="PDF 涂黑/脱敏（真删除），返回处理后的 PDF 流")
async def redact_pdf(
    file: UploadFile = File(...),
    mode: str = Form(...),
    rects: str = Form("[]"),
    keywords: str = Form("{}"),
    options: str = Form("{}"),
):
    if mode not in ("rects", "keywords"):
        raise PDFProcessingError("mode 必须为 rects 或 keywords")
    rects_list = redactor.parse_rects(_parse_json_form(rects, "rects", []))
    keywords_obj = redactor.parse_keywords(_parse_json_form(keywords, "keywords", {}))
    options_obj = _parse_json_form(options, "options", {})
    if mode == "rects" and not rects_list:
        raise PDFProcessingError("请至少框选一个涂黑区域")
    if mode == "keywords" and not (
        keywords_obj["presets"] or keywords_obj["custom"]
    ):
        raise PDFProcessingError("请至少选择一个预设或输入一个自定义关键词")

    task_id, task_dir = make_task_dir(TEMP_DIR)
    input_path = _save_redact_upload(file, task_dir)
    with open(input_path, "rb") as f:
        pdf_data = f.read()

    try:
        out_bytes, report = redactor.redact(
            pdf_data,
            mode,
            rects_list,
            keywords_obj,
            deep_clean=bool(options_obj.get("deepClean", False)),
        )
    except PDFProcessingError:
        raise
    except Exception as e:  # 未知异常统一 422 文案，不泄露内部细节
        logger.error(f"redact task {task_id} failed: {e}")
        raise PDFProcessingError("涂黑处理失败，请重试或更换文件")

    out_path = safe_join(task_dir, "redacted.pdf")
    with open(out_path, "wb") as f:
        f.write(out_bytes)

    report_header = redactor.encode_report(report)
    headers = {"X-Redact-Report": report_header} if report_header else {}

    original_stem = (file.filename or "redacted").rsplit(".", 1)[0][:80] or "redacted"
    logger.info(f"redact task {task_id} completed ({len(out_bytes)} bytes)")
    return FileResponse(
        out_path,
        media_type="application/pdf",
        filename=f"{original_stem}-已涂黑.pdf",
        headers=headers,
    )


@router.post("/locate", summary="关键词定位（只返回计数与页码分布，不返回原文）")
async def redact_locate(
    file: UploadFile = File(...),
    keywords: str = Form("{}"),
):
    keywords_obj = redactor.parse_keywords(_parse_json_form(keywords, "keywords", {}))
    if not (keywords_obj["presets"] or keywords_obj["custom"]):
        raise PDFProcessingError("请至少选择一个预设或输入一个自定义关键词")

    task_id, task_dir = make_task_dir(TEMP_DIR)
    input_path = _save_redact_upload(file, task_dir)
    with open(input_path, "rb") as f:
        pdf_data = f.read()

    result = redactor.locate(pdf_data, "keywords", [], keywords_obj)
    logger.info(f"redact locate task {task_id}: total={result['total']}")
    return RedactLocateResponse(**result)
