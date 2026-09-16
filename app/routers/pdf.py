import os
from pathlib import Path
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
from app.services.pdf_unlocker import PDFUnlocker
from app.services.pdf_page_remover import PDFPageRemover
from app.services.pdf_rotator import PDFRotator
from app.services.pdf_watermarker import PDFWatermarker
from app.services.pdf_organizer import PDFOrganizer, parse_order
from app.services.pdf_header_footer import PDFHeaderFooter
from app.services.pdf_to_word import PDFToWordConverter
from app.services.ofd_validator import (
    OFD_MSG_ENCRYPTED,
    OFD_MSG_NOT_OFD,
    OfdEncryptedError,
    OfdFileError,
    convert_ofd_to_pdf,
    validate_ofd_zip,
)
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


@router.post("/unlock", response_model=TaskResponse, summary="Remove password protection from a PDF")
async def unlock_pdf(
    file: UploadFile = File(...),
    password: str = Form(""),
):
    validate_pdf(file)
    task_id, task_dir = make_task_dir(TEMP_DIR)

    input_path = save_pdf(file, task_dir)

    try:
        output_path = safe_join(task_dir, "unlocked.pdf")
        unlocker = PDFUnlocker(input_path)
        result = unlocker.unlock(output_path, password=password)

        download_url = f"/api/v1/pdf/download/{task_id}"
        message = (
            "密码保护已解除" if result["was_encrypted"] else "该 PDF 未设置密码，已直接输出原文件"
        )

        logger.info(f"Unlock-PDF task {task_id} completed: {file.filename} (was_encrypted={result['was_encrypted']})")
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message=message,
            download_url=download_url,
            file_count=1,
        )
    except ValueError as e:
        logger.warning(f"Unlock-PDF task {task_id} rejected: {e}")
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        logger.error(f"Unlock-PDF task {task_id} failed: {e}")
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
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"PDF analyze task {task_id} failed: {e}")
        # pypdf 对加密文件在 len(reader.pages) 时抛"not been decrypted"，
        # 单独识别给出可操作提示（引导到解锁工具），而非 500
        msg = str(e).lower()
        if "decrypt" in msg or "password" in msg or "encrypt" in msg:
            raise HTTPException(
                status_code=400,
                detail="PDF 已加密，请先用「解除 PDF 密码」工具解密后再上传",
            )
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


@router.post("/rotate", response_model=TaskResponse, summary="Rotate pages of a PDF")
async def rotate_pdf(
    task_id: str = Form(...),
    angle: int = Form(...),
    pages: str = Form(""),
):
    task_dir = get_task_dir(TEMP_DIR, task_id)
    if not os.path.exists(task_dir):
        raise HTTPException(status_code=404, detail="任务不存在或已过期，请重新上传")

    input_path = safe_join(task_dir, "input.pdf")
    if not os.path.exists(input_path):
        raise HTTPException(status_code=404, detail="未找到 PDF 文件，请重新上传")

    if angle not in (90, 180, 270):
        raise HTTPException(status_code=400, detail="旋转角度只支持 90、180、270")

    try:
        rotator = PDFRotator(input_path)
        total_pages = rotator.total_pages

        pages_set = None
        if pages.strip():
            pages_set = PDFRotator.parse_page_list(pages, total_pages)
            if not pages_set:
                raise HTTPException(status_code=400, detail="未指定有效的旋转页码")

        output_path = safe_join(task_dir, "rotated.pdf")
        total, rotated = rotator.rotate(angle, output_path, pages=pages_set)

        if os.path.exists(input_path):
            os.remove(input_path)

        download_url = f"/api/v1/pdf/download/{task_id}"
        scope = f"指定 {rotated} 页" if pages_set else "全部页面"
        logger.info(f"Rotate task {task_id} completed: {scope} by {angle} degrees")
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message=f"已将{scope}顺时针旋转 {angle} 度（共 {total} 页）",
            download_url=download_url,
            file_count=1,
        )
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Rotate task {task_id} failed: {e}")
        raise_processing_error(e)


@router.post("/watermark", response_model=TaskResponse, summary="Add a text or image watermark to a PDF")
async def watermark_pdf(
    task_id: str = Form(...),
    wm_type: str = Form("text"),
    text: str = Form(""),
    layout: str = Form("tile"),
    opacity: float = Form(0.2),
    color: str = Form("gray"),
    fontsize: float = Form(48.0),
    width_fraction: float = Form(0.3),
    pages: str = Form(""),
    wm_image: UploadFile = File(None),
):
    task_dir = get_task_dir(TEMP_DIR, task_id)
    if not os.path.exists(task_dir):
        raise HTTPException(status_code=404, detail="任务不存在或已过期，请重新上传")

    input_path = safe_join(task_dir, "input.pdf")
    if not os.path.exists(input_path):
        raise HTTPException(status_code=404, detail="未找到 PDF 文件，请重新上传")

    if wm_type not in ("text", "image"):
        raise HTTPException(status_code=400, detail="水印类型只支持 text / image")

    try:
        watermarker = PDFWatermarker(input_path)
        total_pages = watermarker.total_pages

        pages_set = None
        if pages.strip():
            pages_set = PDFWatermarker.parse_page_list(pages, total_pages)
            if not pages_set:
                raise HTTPException(status_code=400, detail="未指定有效的水印页码")

        output_path = safe_join(task_dir, "watermarked.pdf")

        if wm_type == "text":
            total, applied = watermarker.watermark_text(
                text,
                output_path,
                opacity=opacity,
                layout=layout,
                color=color,
                fontsize=fontsize,
                pages=pages_set,
            )
        else:
            if wm_image is None or not wm_image.filename:
                raise HTTPException(status_code=400, detail="请上传水印图片")
            ext = validate_extension(wm_image.filename, (".png", ".jpg", ".jpeg"))
            image_bytes = await wm_image.read()
            if len(image_bytes) > 10 * 1024 * 1024:
                raise HTTPException(status_code=400, detail="水印图片不能超过 10MB")
            # 魔数校验（与 file_security.validate_file_header 同语义，作用于已读 bytes）
            magic = image_bytes[:16]
            if ext == ".png" and not magic.startswith(b"\x89PNG\r\n\x1a\n"):
                raise HTTPException(status_code=400, detail="水印图片文件内容与扩展名不符")
            if ext in (".jpg", ".jpeg") and not magic.startswith(b"\xff\xd8"):
                raise HTTPException(status_code=400, detail="水印图片文件内容与扩展名不符")
            total, applied = watermarker.watermark_image(
                image_bytes,
                output_path,
                opacity=opacity,
                layout=layout,
                width_fraction=width_fraction,
                pages=pages_set,
            )

        if os.path.exists(input_path):
            os.remove(input_path)

        download_url = f"/api/v1/pdf/download/{task_id}"
        scope = f"指定 {applied} 页" if pages_set else "全部页面"
        kind = "文字" if wm_type == "text" else "图片"
        logger.info(f"Watermark task {task_id} completed: {kind} watermark on {scope}")
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message=f"已给{scope}添加{kind}水印（共 {total} 页）",
            download_url=download_url,
            file_count=1,
        )
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Watermark task {task_id} failed: {e}")
        raise_processing_error(e)


@router.post("/organize", response_model=TaskResponse, summary="Reorganize pages of a PDF (reorder / delete / rotate)")
async def organize_pdf(
    task_id: str = Form(...),
    order: str = Form(""),
):
    """按前端传来的输出页序重建文档。

    order 形如 "3,1,2"（重排）、"1,3,5"（删除第 2/4 页）、"2:90,1"（单页旋转），
    每项为 "N" 或 "N:angle"（顺时针叠加角度）。
    """
    task_dir = get_task_dir(TEMP_DIR, task_id)
    if not os.path.exists(task_dir):
        raise HTTPException(status_code=404, detail="任务不存在或已过期，请重新上传")

    input_path = safe_join(task_dir, "input.pdf")
    if not os.path.exists(input_path):
        raise HTTPException(status_code=404, detail="未找到 PDF 文件，请重新上传")

    try:
        organizer = PDFOrganizer(input_path)
        total_pages = organizer.total_pages

        entries = parse_order(order, total_pages)

        output_path = safe_join(task_dir, "organized.pdf")
        src_total, out_total = organizer.organize(entries, output_path)

        if os.path.exists(input_path):
            os.remove(input_path)

        download_url = f"/api/v1/pdf/download/{task_id}"
        logger.info(
            f"Organize task {task_id} completed: {src_total} -> {out_total} pages"
        )
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message=f"整理完成：{src_total} 页 -> {out_total} 页",
            download_url=download_url,
            file_count=1,
        )
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Organize task {task_id} failed: {e}")
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


@router.post("/header-footer", response_model=TaskResponse, summary="Add header, footer and page numbers to a PDF")
async def add_header_footer(
    file: UploadFile = File(...),
    header_text: str = Form(""),
    header_position: str = Form("header-center"),
    footer_text: str = Form(""),
    footer_position: str = Form("footer-center"),
    page_number_position: str = Form("header-right"),
    page_number_format: str = Form("plain"),
    page_number_start: int = Form(1),
    margin: float = Form(36.0),
    apply_from_page: int = Form(1),
    header_font: str = Form("song"),
    header_font_size: float = Form(9.0),
    header_color: str = Form("#000000"),
    footer_font: str = Form("song"),
    footer_font_size: float = Form(9.0),
    footer_color: str = Form("#000000"),
    page_font: str = Form("song"),
    page_font_size: float = Form(9.0),
    page_color: str = Form("#000000"),
):
    if not any([header_text.strip(), footer_text.strip(), page_number_position != "none"]):
        raise HTTPException(status_code=400, detail="请至少设置页眉、页脚或页码中的一项")

    validate_pdf(file)
    task_id, task_dir = make_task_dir(TEMP_DIR)

    input_path = save_pdf(file, task_dir)

    try:
        output_path = safe_join(task_dir, "header-footer.pdf")
        hf = PDFHeaderFooter(input_path)
        info = hf.apply(
            output_path,
            header_text=header_text.strip() or None,
            header_position=header_position,
            footer_text=footer_text.strip() or None,
            footer_position=footer_position,
            page_number_position=page_number_position,
            page_number_format=page_number_format,
            page_number_start=page_number_start,
            margin=margin,
            apply_from_page=apply_from_page,
            header_font=header_font,
            header_font_size=header_font_size,
            header_color=header_color,
            footer_font=footer_font,
            footer_font_size=footer_font_size,
            footer_color=footer_color,
            page_font=page_font,
            page_font_size=page_font_size,
            page_color=page_color,
        )

        download_url = f"/api/v1/pdf/download/{task_id}"

        parts = []
        if info["headers_applied"]:
            parts.append(f"页眉×{info['headers_applied']}页")
        if info["footers_applied"]:
            parts.append(f"页脚×{info['footers_applied']}页")
        if info["numbers_applied"]:
            parts.append(f"页码×{info['numbers_applied']}页")

        logger.info(f"Header-footer task {task_id} completed: {', '.join(parts)}")
        return TaskResponse(
            task_id=task_id,
            status="completed",
            message=f"已添加 {'、'.join(parts)}，共 {info['page_count']} 页",
            download_url=download_url,
            file_count=1,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Header-footer task {task_id} failed: {e}")
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
            # 统一命名为 converted.jpg/png：下载端点按 generic 名替换为「原始文件主干 + 扩展名」
            ext = os.path.splitext(image_paths[0])[1] or ".jpg"
            final_path = safe_join(task_dir, f"converted{ext}")
            os.replace(image_paths[0], final_path)
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

    # zip 预扫描（zip bomb 四规则 + 加密检测），与 /ofd/view 共用同一校验
    # （docs/OFD_VIEWER_DESIGN.md §4.3：顺带加固本端点）
    try:
        validate_ofd_zip(input_path)
    except OfdEncryptedError:
        logger.info(f"OFD-to-PDF task {task_id} rejected: encrypted file")
        raise HTTPException(status_code=422, detail=OFD_MSG_ENCRYPTED)
    except OfdFileError as e:
        logger.info(f"OFD-to-PDF task {task_id} rejected: {e}")
        raise HTTPException(status_code=422, detail=OFD_MSG_NOT_OFD)

    try:
        output_path = safe_join(task_dir, "converted.pdf")
        # crop=False（P2-4）：下载链路只做整页等比缩放，不缩角裁剪——
        # 下载文件的物理尺寸应忠实于原文档（页边距保留，打印表现一致）
        success, result = await convert_ofd_to_pdf(
            input_path, output_path, crop=False
        )

        if not success:
            logger.error(f"OFD-to-PDF task {task_id} failed: {result}")
            raise HTTPException(
                status_code=422,
                detail=(
                    f"OFD 转换失败：{result or '未知错误'}。"
                    "该文件可能包含不受支持的电子签章或版式特性，"
                    "请确认其为标准 OFD 文件后重试。"
                ),
            )

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

    # 总量守卫：单文件各 ≤ MAX_UPLOAD_SIZE，但 20 × 50MB 合计可达 1GB
    total_size = sum(f.size or 0 for f in files)
    if total_size > 2 * settings.MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=400,
            detail="所有图片加起来不能超过 100MB，请分批处理",
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

    # 单文件上传时，用「原始文件主干 + 输出扩展名」作为下载名，
    # 而不是 converted.pdf 这类机器名（服务端 Content-Disposition
    # 优先级高于前端 <a download> 属性，必须在服务端改名）
    original_stem = _read_single_upload_stem(task_dir)

    # Find the output file (zip or single pdf)
    candidates = [
        ("merged.pdf", "merged.pdf"),
        ("compressed.pdf", "compressed.pdf"),
        ("converted.pdf", "converted.pdf"),
        ("images.pdf", "images.pdf"),
        ("protected.pdf", "protected.pdf"),
        ("unlocked.pdf", "unlocked.pdf"),
        ("removed.pdf", "removed.pdf"),
        ("merged_invoices.pdf", "发票合并打印.pdf"),
        ("output.md", "output.md"),
        (f"{task_id}.zip", "split-result.zip"),
        ("converted.jpg", "converted.jpg"),
        ("converted.png", "converted.png"),
        ("converted.docx", "converted.docx"),
        ("converted.md", "converted.md"),
        ("images.zip", "images.zip"),
        ("extracted-images.zip", "extracted-images.zip"),
    ]
    for c, download_name in candidates:
        path = safe_join(task_dir, c)
        if os.path.exists(path):
            return FileResponse(
                path,
                media_type="application/octet-stream",
                filename=_derive_download_name(download_name, original_stem),
            )

    # Fallback: first pdf/zip in dir
    for f in os.listdir(task_dir):
        if f.endswith((".pdf", ".zip")):
            download_name = "result.zip" if f.endswith(".zip") else "result.pdf"
            return FileResponse(
                safe_join(task_dir, f),
                media_type="application/octet-stream",
                filename=_derive_download_name(download_name, original_stem),
            )

    raise HTTPException(status_code=404, detail="Output file not found")


# 泛用的机器输出名：单文件上传时替换为原始文件名主干
_GENERIC_OUTPUT_STEMS = {
    "converted", "merged", "compressed", "images", "output",
    "protected", "unlocked", "removed", "result", "split-result",
    "extracted-images", "header-footer", "organized", "rotated", "watermarked",
}


def _read_single_upload_stem(task_dir: str) -> str | None:
    """读取任务目录的 upload_names.txt；仅当恰好记录了一个文件时返回其主干名。"""
    names_path = safe_join(task_dir, "upload_names.txt")
    if not os.path.exists(names_path):
        return None
    try:
        with open(names_path, encoding="utf-8") as f:
            names = [ln.strip() for ln in f if ln.strip()]
        if len(names) != 1:
            return None
        stem = Path(names[0]).stem.strip()
        return stem or None
    except Exception:
        return None


def _derive_download_name(download_name: str, original_stem: str | None) -> str:
    """把泛用输出名（converted.pdf 等）换成「原始文件主干 + 扩展名」。"""
    if not original_stem:
        return download_name
    base = Path(download_name).stem
    if base in _GENERIC_OUTPUT_STEMS:
        return original_stem + Path(download_name).suffix
    return download_name
