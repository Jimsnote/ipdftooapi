from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from app.config import settings
from app.routers import pdf, health, id_photo, invoice, ocr
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

# Routers
app.include_router(health.router, prefix="/api/v1", tags=["Health"])
app.include_router(pdf.router, prefix="/api/v1/pdf", tags=["PDF"])
app.include_router(id_photo.router, prefix="/api/v1/id-photo", tags=["ID Photo"])
app.include_router(invoice.router, prefix="/api/v1/invoice", tags=["Invoice"])
app.include_router(ocr.router, prefix="/api/v1/ocr", tags=["OCR"])
