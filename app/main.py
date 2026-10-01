import math

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager

from app.config import settings
from app.routers import pdf, health, id_photo, invoice, invoice_extract, ocr, ofd, ofd_invoice, redact, visa
from app.core.logger import get_logger

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting {settings.APP_NAME} v{settings.APP_VERSION}")
    yield
    logger.info(f"Shutting down {settings.APP_NAME}")


app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="iPDFToo API - PDF processing backend",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _sanitize_non_finite(value):
    """递归清洗非有限浮点（NaN/Infinity → None），保证 422 详情可 JSON 序列化。

    审计 R1：pydantic 2.13 的 errors() 携带原始 input 值；JSON body 收到字面量
    NaN/Infinity（非标准 JSON，但 Python json.loads 默认放行）时，schema 校验
    虽正确拒绝，但框架默认 422 处理器序列化时抛
    "Out of range float values are not JSON compliant" → 裸 500。
    """
    if isinstance(value, dict):
        return {k: _sanitize_non_finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_non_finite(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


# 审计 P3 英文错误族：pydantic 422 详情中文化（此前全英文直出，
# 如 protect 的 7 个 allow_* 布尔、rotate angle 数值等）
_VALIDATION_TYPE_MSG = {
    "missing": "缺少必填参数",
    "extra_forbidden": "包含不支持的参数",
    "string_type": "应为文本",
    "string_too_long": "内容过长",
    "string_too_short": "内容过短",
    "bool_type": "应为布尔值（true/false）",
    "int_type": "应为整数",
    "int_parsing": "应为整数",
    "float_type": "应为数字",
    "float_parsing": "应为数字",
    "number_type": "应为数字",
    "greater_than_equal": "数值低于下限",
    "less_than_equal": "数值超过上限",
    "greater_than": "数值低于下限",
    "less_than": "数值超过上限",
    "finite_number": "数值无效（不接受 NaN/Infinity）",
    "json_invalid": "请求体不是合法 JSON",
    "json_type": "请求体格式不正确",
    "list_type": "应为列表",
    "dict_type": "应为对象",
    "value_error": "参数不合法",
}


def _translate_validation_errors(errors: list) -> list:
    """把 pydantic 错误详情的 msg 翻译为中文（未知类型给通用文案）。"""
    translated = []
    for err in errors:
        if not isinstance(err, dict):
            translated.append(err)
            continue
        item = dict(err)
        msg = _VALIDATION_TYPE_MSG.get(str(err.get("type")))
        if msg:
            item["msg"] = msg
        translated.append(item)
    return translated


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
    # R1：清洗错误详情中的非有限浮点后返回标准 422 JSON，替代会自崩的默认处理器
    # P3：错误 msg 同步中文化
    return JSONResponse(
        status_code=422,
        content={
            "detail": _translate_validation_errors(
                _sanitize_non_finite(jsonable_encoder(exc.errors()))
            )
        },
    )


# Routers
app.include_router(health.router, prefix="/api/v1", tags=["Health"])
app.include_router(pdf.router, prefix="/api/v1/pdf", tags=["PDF"])
app.include_router(id_photo.router, prefix="/api/v1/id-photo", tags=["ID Photo"])
app.include_router(invoice.router, prefix="/api/v1/invoice", tags=["Invoice"])
app.include_router(invoice_extract.router, prefix="/api/v1/invoice", tags=["Invoice Extract"])
app.include_router(ocr.router, prefix="/api/v1/ocr", tags=["OCR"])
app.include_router(ofd.router, prefix="/api/v1/ofd", tags=["OFD"])
app.include_router(ofd_invoice.router, prefix="/api/v1/ofd-invoice", tags=["OFD Invoice"])
app.include_router(visa.router, prefix="/api/v1/visa", tags=["Visa Form"])
app.include_router(redact.router, prefix="/api/v1/redact", tags=["Redact"])
