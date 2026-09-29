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


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
    # R1：清洗错误详情中的非有限浮点后返回标准 422 JSON，替代会自崩的默认处理器
    return JSONResponse(
        status_code=422,
        content={"detail": _sanitize_non_finite(jsonable_encoder(exc.errors()))},
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
