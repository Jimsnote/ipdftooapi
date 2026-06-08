import os
import uuid
import shutil
from typing import List
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse

from app.config import settings
from app.models.schemas import TaskResponse, DownloadResponse
from app.services.pdf_splitter import PDFSplitter
from app.services.pdf_merger import PDFMerger
from app.services.pdf_compressor import PDFCompressor
from app.services.pdf_converter import PDFToMarkdownConverter
from app.services.word_converter import WordConverter
from app.services.pdf_to_image import PDFToImageConverter
from app.services.image_to_pdf import ImageToPDFConverter
from app.services.storage import StorageService
from app.core.logger import get_logger
from app.core.exceptions import FileTooLargeError, InvalidFileTypeError

router = APIRouter()
logger = get_logger(__name__)

TEMP_DIR = "/app/temp" if os.path.exists("/app/temp") else "./temp"
os.makedirs(TEMP_DIR, exist_ok=True)

storage = StorageService()


def validate_pdf(file: UploadFile):
    if file.size and file.size > settings.MAX_UPLOAD_SIZE:
        raise FileTooLargeError(settings.MAX_UPLOAD_SIZE)
    if file.content_type != "application/pdf":
        # Fallback: check extension
        if not file.filename or not file.filename.lower().endswith(".pdf"):
            raise InvalidFileTypeError()


@router.post("/split", response_model=TaskResponse, summary="Split a PDF file")
async def split_pdf(
    file: UploadFile = File(...),
    mode: str = Form("all"),
    value: str = Form(""),
):
    validate_pdf(file)
    task_id = str(uuid.uuid4())
    task_dir = os.path.join(TEMP_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    input_path = os.path.join(task_dir, file.filename or "input.pdf")
    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        splitter = PDFSplitter(input_path)
        output_paths = splitter.split(mode=mode, value=value, output_dir=task_dir)

        # For single output, return directly; for multiple, zip them
        if len(output_paths) == 1:
            final_path = output_paths[0]
        else:
            final_path = os.path.join(task_dir, f"{task_id}.zip")
            import zipfile

            with zipfile.ZipFile(final_path, "w") as zf:
                for p in output_paths:
                    zf.write(p, os.path.basename(p))

        # TODO: Upload to COS and return presigned URL
        # download_url = storage.upload_and_get_url(final_path, f"tasks/{task_id}")
        download_url = f"/api/v1/pdf/download/{task_id}"

        logger.info(f"Split task {task_id} completed: {len(output_paths)} files")
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message=f"PDF split into {len(output_paths)} file(s)",
            download_url=download_url,
            file_count=len(output_paths),
        )
    except Exception as e:
        logger.error(f"Split task {task_id} failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/merge", response_model=TaskResponse, summary="Merge multiple PDF files")
async def merge_pdf(
    files: List[UploadFile] = File(...),
):
    if len(files) > settings.MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {settings.MAX_FILES_PER_REQUEST} files allowed",
        )

    task_id = str(uuid.uuid4())
    task_dir = os.path.join(TEMP_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    input_paths = []
    for f in files:
        validate_pdf(f)
        path = os.path.join(task_dir, f.filename or f"input_{len(input_paths)}.pdf")
        with open(path, "wb") as out:
            shutil.copyfileobj(f.file, out)
        input_paths.append(path)

    try:
        output_path = os.path.join(task_dir, "merged.pdf")
        merger = PDFMerger(input_paths)
        merger.merge(output_path)

        # TODO: Upload to COS
        download_url = f"/api/v1/pdf/download/{task_id}"

        logger.info(f"Merge task {task_id} completed: {len(input_paths)} files")
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message=f"Merged {len(input_paths)} PDF file(s)",
            download_url=download_url,
            file_count=1,
        )
    except Exception as e:
        logger.error(f"Merge task {task_id} failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/download/{task_id}", summary="Download processed file")
async def download_file(task_id: str):
    task_dir = os.path.join(TEMP_DIR, task_id)
    if not os.path.exists(task_dir):
        raise HTTPException(status_code=404, detail="Task not found or expired")

    # Find the output file (zip or single pdf)
    candidates = [
        ("merged.pdf", "merged.pdf"),
        ("compressed.pdf", "compressed.pdf"),
        ("output.md", "output.md"),
        (f"{task_id}.zip", "split-result.zip"),
    ]
    for c, download_name in candidates:
        path = os.path.join(task_dir, c)
        if os.path.exists(path):
            return FileResponse(
                path,
                media_type="application/octet-stream",
                filename=download_name,
            )

    # Fallback: first pdf/zip in dir
    for f in os.listdir(task_dir):
        if f.endswith((".pdf", ".zip")):
            download_name = "result.zip" if f.endswith(".zip") else "result.pdf"
            return FileResponse(
                os.path.join(task_dir, f),
                media_type="application/octet-stream",
                filename=download_name,
            )

@router.post("/compress", response_model=TaskResponse, summary="Compress a PDF file")
async def compress_pdf(
    file: UploadFile = File(...),
    level: str = Form("normal"),
):
    validate_pdf(file)
    task_id = str(uuid.uuid4())
    task_dir = os.path.join(TEMP_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    input_path = os.path.join(task_dir, file.filename or "input.pdf")
    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        output_path = os.path.join(task_dir, "compressed.pdf")
        compressor = PDFCompressor(input_path)
        compressor.compress(output_path, level=level)

        download_url = f"/api/v1/pdf/download/{task_id}"

        original_size = os.path.getsize(input_path)
        compressed_size = os.path.getsize(output_path)
        reduction = round((1 - compressed_size / original_size) * 100, 1) if original_size > 0 else 0

        if reduction > 0:
            msg = f"PDF 压缩完成，体积减小 {reduction}%"
        else:
            msg = f"PDF 处理完成（当前文档已高度优化，进一步压缩空间有限）"

        logger.info(f"Compress task {task_id} completed: {original_size} -> {compressed_size} ({reduction}% reduction)")
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message=msg,
            download_url=download_url,
            file_count=1,
        )
    except Exception as e:
        logger.error(f"Compress task {task_id} failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/download/{task_id}", summary="Download processed file")
async def download_file(task_id: str):
    task_dir = os.path.join(TEMP_DIR, task_id)
    if not os.path.exists(task_dir):
        raise HTTPException(status_code=404, detail="Task not found or expired")

    # Find the output file (zip or single pdf)
    candidates = [
        ("merged.pdf", "merged.pdf"),
        ("compressed.pdf", "compressed.pdf"),
        (f"{task_id}.zip", "split-result.zip"),
    ]
    for c, download_name in candidates:
        path = os.path.join(task_dir, c)
        if os.path.exists(path):
            return FileResponse(
                path,
                media_type="application/octet-stream",
                filename=download_name,
            )

    # Fallback: first pdf/zip in dir
    for f in os.listdir(task_dir):
        if f.endswith((".pdf", ".zip")):
            download_name = "result.zip" if f.endswith(".zip") else "result.pdf"
            return FileResponse(
                os.path.join(task_dir, f),
                media_type="application/octet-stream",
                filename=download_name,
            )

@router.post("/to-markdown", response_model=TaskResponse, summary="Convert a PDF file to Markdown")
async def pdf_to_markdown(
    file: UploadFile = File(...),
    pages: str = Form("all"),
):
    validate_pdf(file)
    task_id = str(uuid.uuid4())
    task_dir = os.path.join(TEMP_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    input_path = os.path.join(task_dir, file.filename or "input.pdf")
    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        output_path = os.path.join(task_dir, "output.md")
        converter = PDFToMarkdownConverter()
        info = converter.convert(input_path, output_path, pages=pages if pages != "all" else None)

        download_url = f"/api/v1/pdf/download/{task_id}"

        logger.info(f"Markdown task {task_id} completed: {info['char_count']} chars")
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message=f"PDF 已转换为 Markdown，共 {info['page_count']} 页，{info['char_count']} 字符",
            download_url=download_url,
            file_count=1,
        )
    except Exception as e:
        logger.error(f"Markdown task {task_id} failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


def validate_word(file: UploadFile):
    if file.size and file.size > settings.MAX_UPLOAD_SIZE:
        raise FileTooLargeError(settings.MAX_UPLOAD_SIZE)
    if file.content_type not in (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    ):
        if not file.filename or not file.filename.lower().endswith((".docx", ".doc")):
            raise InvalidFileTypeError()


def validate_ppt(file: UploadFile):
    if file.size and file.size > settings.MAX_UPLOAD_SIZE:
        raise FileTooLargeError(settings.MAX_UPLOAD_SIZE)
    if file.content_type not in (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-powerpoint",
    ):
        if not file.filename or not file.filename.lower().endswith((".pptx", ".ppt")):
            raise InvalidFileTypeError()


@router.post("/word-to-pdf", response_model=TaskResponse, summary="Convert a Word document to PDF")
async def word_to_pdf(
    file: UploadFile = File(...),
):
    validate_word(file)
    task_id = str(uuid.uuid4())
    task_dir = os.path.join(TEMP_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    input_path = os.path.join(task_dir, file.filename or "input.docx")
    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        output_path = os.path.join(task_dir, "converted.pdf")
        converter = WordConverter(input_path)
        converter.convert(output_path)

        download_url = f"/api/v1/pdf/download/{task_id}"

        logger.info(f"Word-to-PDF task {task_id} completed: {file.filename}")
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message="Word 文档已转换为 PDF",
            download_url=download_url,
            file_count=1,
        )
    except Exception as e:
        logger.error(f"Word-to-PDF task {task_id} failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/ppt-to-pdf", response_model=TaskResponse, summary="Convert a PowerPoint presentation to PDF")
async def ppt_to_pdf(
    file: UploadFile = File(...),
):
    validate_ppt(file)
    task_id = str(uuid.uuid4())
    task_dir = os.path.join(TEMP_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    input_path = os.path.join(task_dir, file.filename or "input.pptx")
    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        output_path = os.path.join(task_dir, "converted.pdf")
        converter = WordConverter(input_path)
        converter.convert(output_path)

        download_url = f"/api/v1/pdf/download/{task_id}"

        logger.info(f"PPT-to-PDF task {task_id} completed: {file.filename}")
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message="PowerPoint 已转换为 PDF",
            download_url=download_url,
            file_count=1,
        )
    except Exception as e:
        logger.error(f"PPT-to-PDF task {task_id} failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/to-jpg", response_model=TaskResponse, summary="Convert PDF pages to images")
async def pdf_to_jpg(
    file: UploadFile = File(...),
    format: str = Form("jpg"),
    pages: str = Form("all"),
):
    validate_pdf(file)
    task_id = str(uuid.uuid4())
    task_dir = os.path.join(TEMP_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    input_path = os.path.join(task_dir, file.filename or "input.pdf")
    with open(input_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        converter = PDFToImageConverter(input_path)
        image_paths = converter.convert(
            output_dir=task_dir,
            format="jpg" if format == "jpg" else "png",
            pages=pages,
        )

        if len(image_paths) == 1:
            final_path = image_paths[0]
            download_url = f"/api/v1/pdf/download/{task_id}"
        else:
            final_path = os.path.join(task_dir, "images.zip")
            PDFToImageConverter.zip_images(image_paths, final_path)
            download_url = f"/api/v1/pdf/download/{task_id}"

        logger.info(f"PDF-to-JPG task {task_id} completed: {len(image_paths)} image(s)")
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message=f"PDF 已转换为 {len(image_paths)} 张图片",
            download_url=download_url,
            file_count=len(image_paths),
        )
    except Exception as e:
        logger.error(f"PDF-to-JPG task {task_id} failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/from-jpg", response_model=TaskResponse, summary="Convert images to PDF")
async def jpg_to_pdf(
    files: List[UploadFile] = File(...),
    orientation: str = Form("portrait"),
    fit_mode: str = Form("fit"),
):
    if len(files) > settings.MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {settings.MAX_FILES_PER_REQUEST} files allowed",
        )

    task_id = str(uuid.uuid4())
    task_dir = os.path.join(TEMP_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)

    input_paths = []
    for f in files:
        if f.size and f.size > settings.MAX_UPLOAD_SIZE:
            raise FileTooLargeError(settings.MAX_UPLOAD_SIZE)
        path = os.path.join(task_dir, f.filename or f"input_{len(input_paths)}.jpg")
        with open(path, "wb") as out:
            shutil.copyfileobj(f.file, out)
        input_paths.append(path)

    try:
        output_path = os.path.join(task_dir, "images.pdf")
        converter = ImageToPDFConverter()
        converter.convert(
            image_paths=input_paths,
            output_path=output_path,
            orientation="portrait" if orientation == "portrait" else "landscape",
            fit_mode="fit" if fit_mode == "fit" else ("fill" if fit_mode == "fill" else "original"),
        )

        download_url = f"/api/v1/pdf/download/{task_id}"

        logger.info(f"JPG-to-PDF task {task_id} completed: {len(input_paths)} image(s)")
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message=f"已将 {len(input_paths)} 张图片合并为 PDF",
            download_url=download_url,
            file_count=1,
        )
    except Exception as e:
        logger.error(f"JPG-to-PDF task {task_id} failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/download/{task_id}", summary="Download processed file")
async def download_file(task_id: str):
    task_dir = os.path.join(TEMP_DIR, task_id)
    if not os.path.exists(task_dir):
        raise HTTPException(status_code=404, detail="Task not found or expired")

    # Find the output file (zip or single pdf)
    candidates = [
        ("merged.pdf", "merged.pdf"),
        ("compressed.pdf", "compressed.pdf"),
        ("converted.pdf", "converted.pdf"),
        ("images.pdf", "images.pdf"),
        ("output.md", "output.md"),
        (f"{task_id}.zip", "split-result.zip"),
        ("images.zip", "images.zip"),
    ]
    for c, download_name in candidates:
        path = os.path.join(task_dir, c)
        if os.path.exists(path):
            return FileResponse(
                path,
                media_type="application/octet-stream",
                filename=download_name,
            )

    # Fallback: first pdf/zip in dir
    for f in os.listdir(task_dir):
        if f.endswith((".pdf", ".zip")):
            download_name = "result.zip" if f.endswith(".zip") else "result.pdf"
            return FileResponse(
                os.path.join(task_dir, f),
                media_type="application/octet-stream",
                filename=download_name,
            )

    raise HTTPException(status_code=404, detail="Output file not found")
