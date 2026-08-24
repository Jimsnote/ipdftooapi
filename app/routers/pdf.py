import os
from typing import List
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import FileResponse

from app.config import settings
from app.models.schemas import TaskResponse
from app.services.pdf_splitter import PDFSplitter
from app.services.pdf_merger import PDFMerger
from app.services.pdf_compressor import PDFCompressor
from app.services.pdf_converter import PDFToMarkdownConverter
from app.services.word_converter import WordConverter
from app.services.pdf_to_image import PDFToImageConverter
from app.services.pdf_image_extractor import PDFImageExtractor
from app.services.image_to_pdf import ImageToPDFConverter
from app.services.pdf_protector import PDFProtector
from app.services.pdf_page_remover import PDFPageRemover
from app.services.pdf_to_word import PDFToWordConverter
from app.services.ofd_converter import OFDConverter
from app.services.markdown_converter import OfficeToMarkdownConverter
from app.services.storage import StorageService
from app.core.logger import get_logger
from app.core.exceptions import FileTooLargeError
from app.core.file_security import (
    get_task_dir,
    make_task_dir,
    safe_join,
    save_upload_file,
    validate_file_header,
    validate_extension,
)

router = APIRouter()
logger = get_logger(__name__)

TEMP_DIR = os.path.abspath("/app/temp" if os.path.exists("/app/temp") else "./temp")
os.makedirs(TEMP_DIR, exist_ok=True)

storage = StorageService()

PDF_EXTENSIONS = (".pdf",)
WORD_EXTENSIONS = (".docx", ".doc")
PPT_EXTENSIONS = (".pptx", ".ppt")
EXCEL_EXTENSIONS = (".xlsx", ".xls")
OFD_EXTENSIONS = (".ofd",)
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png")


def raise_processing_error(error: Exception):
    if isinstance(error, HTTPException):
        raise error
    if isinstance(error, ValueError):
        raise HTTPException(status_code=400, detail=str(error))
    raise HTTPException(status_code=500, detail=str(error))


def validate_pdf(file: UploadFile):
    if file.size and file.size > settings.MAX_UPLOAD_SIZE:
        raise FileTooLargeError(settings.MAX_UPLOAD_SIZE)
    suffix = validate_extension(file.filename, PDF_EXTENSIONS)
    validate_file_header(file, suffix)


def save_pdf(file: UploadFile, task_dir: str, filename: str = "input.pdf") -> str:
    return save_upload_file(
        file,
        safe_join(task_dir, filename),
        settings.MAX_UPLOAD_SIZE,
        PDF_EXTENSIONS,
    )


@router.post("/split", response_model=TaskResponse, summary="Split a PDF file")
async def split_pdf(
    file: UploadFile = File(...),
    mode: str = Form("all"),
    value: str = Form(""),
):
    validate_pdf(file)
    task_id, task_dir = make_task_dir(TEMP_DIR)

    input_path = save_pdf(file, task_dir)

    try:
        splitter = PDFSplitter(input_path)
        output_paths = splitter.split(mode=mode, value=value, output_dir=task_dir)

        # For single output, return directly; for multiple, zip them
        if len(output_paths) == 1:
            final_path = output_paths[0]
        else:
            final_path = safe_join(task_dir, f"{task_id}.zip")
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
        raise_processing_error(e)


@router.post("/merge", response_model=TaskResponse, summary="Merge multiple PDF files")
async def merge_pdf(
    files: List[UploadFile] = File(...),
):
    if len(files) > settings.MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {settings.MAX_FILES_PER_REQUEST} files allowed",
        )

    task_id, task_dir = make_task_dir(TEMP_DIR)

    input_paths = []
    for f in files:
        validate_pdf(f)
        path = save_pdf(f, task_dir, f"input_{len(input_paths)}.pdf")
        input_paths.append(path)

    try:
        output_path = safe_join(task_dir, "merged.pdf")
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
        raise_processing_error(e)


@router.post("/merge-batch", response_model=TaskResponse, summary="Merge multiple PDF files uploaded one by one")
async def merge_batch(
    file: UploadFile = File(...),
    task_id: str = Form(...),
    index: int = Form(...),
    total: int = Form(...),
):
    """Upload PDFs one by one and merge them when all are received.

    Frontend (e.g. WeChat Mini Program) can only send one file per wx.uploadFile call.
    This endpoint accumulates files and triggers merge on the last upload.
    """
    validate_pdf(file)
    task_dir = get_task_dir(TEMP_DIR, task_id, create=True)
    if total < 1 or total > settings.MAX_FILES_PER_REQUEST or index < 0 or index >= total:
        raise HTTPException(status_code=400, detail="\u4e0a\u4f20\u5e8f\u53f7\u65e0\u6548")

    input_path = save_pdf(file, task_dir, f"{index}.pdf")

    logger.info(f"Merge-batch task {task_id}: received file {index + 1}/{total}")

    # Check if all files have been uploaded
    uploaded = set(os.listdir(task_dir))
    expected = {f"{i}.pdf" for i in range(total)}

    if not (uploaded >= expected):
        return TaskResponse(
            task_id=task_id,
            status="uploading",
            message=f"已上传 {len(uploaded)} / {total} 个文件",
            download_url="",
            file_count=0,
        )

    # All files received, perform merge
    try:
        input_paths = [safe_join(task_dir, f"{i}.pdf") for i in range(total)]
        output_path = safe_join(task_dir, "merged.pdf")
        merger = PDFMerger(input_paths)
        merger.merge(output_path)

        download_url = f"/api/v1/pdf/download/{task_id}"
        logger.info(f"Merge-batch task {task_id} completed: {total} files")
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message=f"PDF 合并完成，共 {total} 个文件",
            download_url=download_url,
            file_count=1,
        )
    except Exception as e:
        logger.error(f"Merge-batch task {task_id} failed: {e}")
        raise_processing_error(e)


@router.post("/protect", response_model=TaskResponse, summary="Protect a PDF file with password encryption")
async def protect_pdf(
    file: UploadFile = File(...),
    password: str = Form(...),
    allow_printing: bool = Form(True),
    allow_modifying: bool = Form(True),
    allow_copying: bool = Form(True),
    allow_annotating: bool = Form(True),
    allow_form_filling: bool = Form(True),
    allow_accessibility_extraction: bool = Form(True),
    allow_assembly: bool = Form(True),
):
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="密码长度至少为 6 位")

    validate_pdf(file)
    task_id, task_dir = make_task_dir(TEMP_DIR)

    input_path = save_pdf(file, task_dir)

    try:
        output_path = safe_join(task_dir, "protected.pdf")
        protector = PDFProtector(input_path)
        protector.protect(
            output_path=output_path,
            user_password=password,
            allow_printing=allow_printing,
            allow_modifying=allow_modifying,
            allow_copying=allow_copying,
            allow_annotating=allow_annotating,
            allow_form_filling=allow_form_filling,
            allow_accessibility_extraction=allow_accessibility_extraction,
            allow_assembly=allow_assembly,
        )

        download_url = f"/api/v1/pdf/download/{task_id}"

        logger.info(f"Protect-PDF task {task_id} completed: {file.filename}")
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message="PDF 已成功加密保护",
            download_url=download_url,
            file_count=1,
        )
    except Exception as e:
        logger.error(f"Protect-PDF task {task_id} failed: {e}")
        raise_processing_error(e)


@router.post("/analyze", summary="Analyze a PDF file and return page count")
async def analyze_pdf(file: UploadFile = File(...)):
    validate_pdf(file)
    task_id, task_dir = make_task_dir(TEMP_DIR)

    input_path = save_pdf(file, task_dir)

    try:
        remover = PDFPageRemover(input_path)
        total_pages = remover.total_pages
        return {
            "task_id": task_id,
            "total_pages": total_pages,
            "filename": file.filename,
        }
    except Exception as e:
        logger.error(f"PDF analyze task {task_id} failed: {e}")
        raise_processing_error(e)


@router.post("/remove-pages", response_model=TaskResponse, summary="Remove pages from a PDF")
async def remove_pages(
    task_id: str = Form(...),
    pages: str = Form(...),
):
    task_dir = get_task_dir(TEMP_DIR, task_id)
    if not os.path.exists(task_dir):
        raise HTTPException(status_code=404, detail="任务不存在或已过期，请重新上传")

    # 明确使用 analyze 接口保存的原始文件
    input_path = safe_join(task_dir, "input.pdf")
    if not os.path.exists(input_path):
        raise HTTPException(status_code=404, detail="未找到 PDF 文件，请重新上传")

    try:
        remover = PDFPageRemover(input_path)
        total_pages = remover.total_pages
        pages_to_remove = PDFPageRemover.parse_page_list(pages, total_pages)

        if not pages_to_remove:
            raise HTTPException(status_code=400, detail="未指定有效的删除页码")
        if len(pages_to_remove) >= total_pages:
            raise HTTPException(status_code=400, detail="不能删除所有页面，至少需要保留一页")

        output_path = safe_join(task_dir, "removed.pdf")
        remaining = remover.remove_pages(pages_to_remove, output_path)

        # 删除原文件，避免下载接口 fallback 时返回原文件
        if os.path.exists(input_path):
            os.remove(input_path)

        download_url = f"/api/v1/pdf/download/{task_id}"

        logger.info(f"Remove-pages task {task_id} completed: removed {len(pages_to_remove)}, remaining {remaining}")
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message=f"已删除 {len(pages_to_remove)} 页，剩余 {remaining} 页",
            download_url=download_url,
            file_count=1,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Remove-pages task {task_id} failed: {e}")
        raise_processing_error(e)


@router.post("/compress", response_model=TaskResponse, summary="Compress a PDF file")
async def compress_pdf(
    file: UploadFile = File(...),
    level: str = Form("normal"),
):
    validate_pdf(file)
    task_id, task_dir = make_task_dir(TEMP_DIR)

    input_path = save_pdf(file, task_dir)

    try:
        output_path = safe_join(task_dir, "compressed.pdf")
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
        raise_processing_error(e)


@router.post("/to-word", response_model=TaskResponse, summary="Convert a PDF file to Word")
async def pdf_to_word(
    file: UploadFile = File(...),
    pages: str = Form("all"),
):
    validate_pdf(file)
    task_id, task_dir = make_task_dir(TEMP_DIR)

    input_path = save_pdf(file, task_dir)

    try:
        output_path = safe_join(task_dir, "converted.docx")
        converter = PDFToWordConverter()
        info = converter.convert(
            input_path, output_path,
            pages=pages if pages != "all" else None
        )

        download_url = f"/api/v1/pdf/download/{task_id}"

        logger.info(f"PDF-to-Word task {task_id} completed: {info['page_count']} pages")
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message=f"PDF 已转换为 Word 文档，共 {info['page_count']} 页",
            download_url=download_url,
            file_count=1,
        )
    except Exception as e:
        logger.error(f"PDF-to-Word task {task_id} failed: {e}")
        raise_processing_error(e)


@router.post("/to-markdown", response_model=TaskResponse, summary="Convert a PDF file to Markdown")
async def pdf_to_markdown(
    file: UploadFile = File(...),
    pages: str = Form("all"),
):
    validate_pdf(file)
    task_id, task_dir = make_task_dir(TEMP_DIR)

    input_path = save_pdf(file, task_dir)

    try:
        output_path = safe_join(task_dir, "output.md")
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
        raise_processing_error(e)


def validate_upload(file: UploadFile, allowed_extensions: tuple[str, ...]):
    if file.size and file.size > settings.MAX_UPLOAD_SIZE:
        raise FileTooLargeError(settings.MAX_UPLOAD_SIZE)
    suffix = validate_extension(file.filename, allowed_extensions)
    validate_file_header(file, suffix)


def validate_word(file: UploadFile):
    validate_upload(file, WORD_EXTENSIONS)


def validate_ppt(file: UploadFile):
    validate_upload(file, PPT_EXTENSIONS)


def validate_excel(file: UploadFile):
    validate_upload(file, EXCEL_EXTENSIONS)


def validate_ofd(file: UploadFile):
    validate_upload(file, OFD_EXTENSIONS)


def save_upload_by_type(
    file: UploadFile,
    task_dir: str,
    filename: str,
    allowed_extensions: tuple[str, ...],
) -> str:
    return save_upload_file(
        file,
        safe_join(task_dir, filename),
        settings.MAX_UPLOAD_SIZE,
        allowed_extensions,
    )


@router.post("/word-to-pdf", response_model=TaskResponse, summary="Convert a Word document to PDF")
async def word_to_pdf(
    file: UploadFile = File(...),
):
    validate_word(file)
    task_id, task_dir = make_task_dir(TEMP_DIR)

    input_path = save_upload_by_type(file, task_dir, "input.docx", WORD_EXTENSIONS)

    try:
        output_path = safe_join(task_dir, "converted.pdf")
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
        raise_processing_error(e)


@router.post("/ppt-to-pdf", response_model=TaskResponse, summary="Convert a PowerPoint presentation to PDF")
async def ppt_to_pdf(
    file: UploadFile = File(...),
):
    validate_ppt(file)
    task_id, task_dir = make_task_dir(TEMP_DIR)

    input_path = save_upload_by_type(file, task_dir, "input.pptx", PPT_EXTENSIONS)

    try:
        output_path = safe_join(task_dir, "converted.pdf")
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
        raise_processing_error(e)


@router.post("/to-jpg", response_model=TaskResponse, summary="Convert PDF pages to images")
async def pdf_to_jpg(
    file: UploadFile = File(...),
    format: str = Form("jpg"),
    pages: str = Form("all"),
):
    validate_pdf(file)
    task_id, task_dir = make_task_dir(TEMP_DIR)

    input_path = save_pdf(file, task_dir)

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
            final_path = safe_join(task_dir, "images.zip")
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
        raise_processing_error(e)


@router.post("/extract-images", response_model=TaskResponse, summary="Extract embedded images from a PDF")
async def extract_images(file: UploadFile = File(...)):
    validate_pdf(file)
    task_id, task_dir = make_task_dir(TEMP_DIR)

    input_path = save_pdf(file, task_dir)

    try:
        extractor = PDFImageExtractor(input_path)
        images = extractor.extract(safe_join(task_dir, "images"))

        if not images:
            raise HTTPException(
                status_code=422,
                detail="未在该 PDF 中找到可提取的图片（可能是纯文字文档）",
            )

        final_path = safe_join(task_dir, "extracted-images.zip")
        PDFImageExtractor.zip_images(images, final_path)
        download_url = f"/api/v1/pdf/download/{task_id}"

        logger.info(f"Extract-images task {task_id} completed: {len(images)} image(s)")
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message=f"已提取 {len(images)} 张图片（原图无损）",
            download_url=download_url,
            file_count=len(images),
        )
    except Exception as e:
        logger.error(f"Extract-images task {task_id} failed: {e}")
        raise_processing_error(e)


@router.post("/ofd-to-pdf", response_model=TaskResponse, summary="Convert an OFD file to PDF")
async def ofd_to_pdf(
    file: UploadFile = File(...),
):
    validate_ofd(file)
    task_id, task_dir = make_task_dir(TEMP_DIR)

    input_path = save_upload_by_type(file, task_dir, "input.ofd", OFD_EXTENSIONS)

    try:
        output_path = safe_join(task_dir, "converted.pdf")
        converter = OFDConverter()
        success, result = converter.ofd_to_pdf(input_path, output_path)

        if not success:
            raise HTTPException(status_code=422, detail=f"OFD 转换失败: {result}")

        download_url = f"/api/v1/pdf/download/{task_id}"

        logger.info(f"OFD-to-PDF task {task_id} completed: {file.filename}")
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message="OFD 文件已成功转换为 PDF",
            download_url=download_url,
            file_count=1,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"OFD-to-PDF task {task_id} failed: {e}")
        raise_processing_error(e)


@router.post("/word-to-markdown", response_model=TaskResponse, summary="Convert a Word document to Markdown")
async def word_to_markdown(
    file: UploadFile = File(...),
):
    validate_word(file)
    task_id, task_dir = make_task_dir(TEMP_DIR)

    input_path = save_upload_by_type(file, task_dir, "input.docx", WORD_EXTENSIONS)

    try:
        output_path = safe_join(task_dir, "converted.md")
        converter = OfficeToMarkdownConverter()
        info = converter.convert(input_path, output_path)

        download_url = f"/api/v1/pdf/download/{task_id}"

        logger.info(f"Word-to-Markdown task {task_id} completed: {info['char_count']} chars")
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message=f"Word 文档已转换为 Markdown，共 {info['char_count']} 字符",
            download_url=download_url,
            file_count=1,
        )
    except Exception as e:
        logger.error(f"Word-to-Markdown task {task_id} failed: {e}")
        raise_processing_error(e)


@router.post("/ppt-to-markdown", response_model=TaskResponse, summary="Convert a PowerPoint presentation to Markdown")
async def ppt_to_markdown(
    file: UploadFile = File(...),
):
    validate_ppt(file)
    task_id, task_dir = make_task_dir(TEMP_DIR)

    input_path = save_upload_by_type(file, task_dir, "input.pptx", PPT_EXTENSIONS)

    try:
        output_path = safe_join(task_dir, "converted.md")
        converter = OfficeToMarkdownConverter()
        info = converter.convert(input_path, output_path)

        download_url = f"/api/v1/pdf/download/{task_id}"

        logger.info(f"PPT-to-Markdown task {task_id} completed: {info['char_count']} chars")
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message=f"PowerPoint 已转换为 Markdown，共 {info['char_count']} 字符",
            download_url=download_url,
            file_count=1,
        )
    except Exception as e:
        logger.error(f"PPT-to-Markdown task {task_id} failed: {e}")
        raise_processing_error(e)


@router.post("/excel-to-markdown", response_model=TaskResponse, summary="Convert an Excel spreadsheet to Markdown")
async def excel_to_markdown(
    file: UploadFile = File(...),
):
    validate_excel(file)
    task_id, task_dir = make_task_dir(TEMP_DIR)

    input_path = save_upload_by_type(file, task_dir, "input.xlsx", EXCEL_EXTENSIONS)

    try:
        output_path = safe_join(task_dir, "converted.md")
        converter = OfficeToMarkdownConverter()
        info = converter.convert(input_path, output_path)

        download_url = f"/api/v1/pdf/download/{task_id}"

        logger.info(f"Excel-to-Markdown task {task_id} completed: {info['char_count']} chars")
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message=f"Excel 表格已转换为 Markdown，共 {info['char_count']} 字符",
            download_url=download_url,
            file_count=1,
        )
    except Exception as e:
        logger.error(f"Excel-to-Markdown task {task_id} failed: {e}")
        raise_processing_error(e)


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

    task_id, task_dir = make_task_dir(TEMP_DIR)

    input_paths = []
    for f in files:
        suffix = validate_extension(f.filename, IMAGE_EXTENSIONS)
        path = save_upload_file(
            f,
            safe_join(task_dir, f"input_{len(input_paths)}{suffix}"),
            settings.MAX_UPLOAD_SIZE,
            IMAGE_EXTENSIONS,
        )
        input_paths.append(path)

    try:
        output_path = safe_join(task_dir, "images.pdf")
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
        raise_processing_error(e)


@router.get("/download/{task_id}", summary="Download processed file")
async def download_file(task_id: str):
    task_dir = get_task_dir(TEMP_DIR, task_id)
    if not os.path.exists(task_dir):
        raise HTTPException(status_code=404, detail="Task not found or expired")

    # Find the output file (zip or single pdf)
    candidates = [
        ("merged.pdf", "merged.pdf"),
        ("compressed.pdf", "compressed.pdf"),
        ("converted.pdf", "converted.pdf"),
        ("images.pdf", "images.pdf"),
        ("protected.pdf", "protected.pdf"),
        ("removed.pdf", "removed.pdf"),
        ("merged_invoices.pdf", "发票合并打印.pdf"),
        ("output.md", "output.md"),
        (f"{task_id}.zip", "split-result.zip"),
        ("converted.docx", "converted.docx"),
        ("converted.md", "converted.md"),
        ("images.zip", "images.zip"),
        ("extracted-images.zip", "PDF提取图片.zip"),
    ]
    for c, download_name in candidates:
        path = safe_join(task_dir, c)
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
                safe_join(task_dir, f),
                media_type="application/octet-stream",
                filename=download_name,
            )

    raise HTTPException(status_code=404, detail="Output file not found")
