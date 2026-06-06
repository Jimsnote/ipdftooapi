import os
import shutil
import subprocess
from typing import Optional

from pypdf import PdfReader, PdfWriter

from app.core.logger import get_logger

logger = get_logger(__name__)


class PDFCompressor:
    """PDF compression with multiple quality levels.

    Uses Ghostscript for best results; falls back to pypdf basic compression
    if Ghostscript is not available.
    """

    LEVELS = {
        "extreme": {
            "label": "极限压缩",
            "description": "最大程度减小文件体积，适合仅需屏幕阅读的场景",
            "gs_setting": "/screen",
            "dpi": 72,
        },
        "normal": {
            "label": "一般压缩（推荐）",
            "description": "平衡文件大小与质量，适合日常办公与邮件传输",
            "gs_setting": "/ebook",
            "dpi": 150,
        },
        "light": {
            "label": "轻度压缩",
            "description": "保留较高图像质量，适合需要打印的文档",
            "gs_setting": "/printer",
            "dpi": 300,
        },
    }

    def __init__(self, file_path: str):
        self.file_path = file_path

    @classmethod
    def get_levels(cls):
        """Return available compression levels for frontend."""
        return {
            k: {"label": v["label"], "description": v["description"]}
            for k, v in cls.LEVELS.items()
        }

    def compress(self, output_path: str, level: str = "normal") -> str:
        if level not in self.LEVELS:
            raise ValueError(f"Unknown compression level: {level}")

        settings = self.LEVELS[level]
        gs_cmd = self._find_gs()

        if gs_cmd:
            return self._compress_with_gs(output_path, settings, gs_cmd)
        else:
            logger.warning("Ghostscript not found, using pypdf fallback compression")
            return self._fallback_compress(output_path)

    def _find_gs(self) -> Optional[str]:
        """Locate Ghostscript executable."""
        for cmd in ["gs", "gsc"]:
            if shutil.which(cmd):
                return cmd
        return None

    def _compress_with_gs(
        self, output_path: str, settings: dict, gs_cmd: str
    ) -> str:
        """Run Ghostscript to compress PDF."""
        dpi = settings["dpi"]
        cmd = [
            gs_cmd,
            "-sDEVICE=pdfwrite",
            "-dCompatibilityLevel=1.4",
            f"-dPDFSETTINGS={settings['gs_setting']}",
            f"-dColorImageResolution={dpi}",
            f"-dGrayImageResolution={dpi}",
            f"-dMonoImageResolution={dpi}",
            "-dDownsampleColorImages=true",
            "-dDownsampleGrayImages=true",
            "-dDownsampleMonoImages=true",
            "-dAutoFilterColorImages=false",
            "-dAutoFilterGrayImages=false",
            "-dColorImageFilter=/DCTEncode",
            "-dGrayImageFilter=/DCTEncode",
            "-dNOPAUSE",
            "-dQUIET",
            "-dBATCH",
            f"-sOutputFile={output_path}",
            self.file_path,
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode != 0:
                logger.error(f"Ghostscript error: {result.stderr}")
                raise RuntimeError(
                    f"PDF compression failed: {result.stderr or 'unknown error'}"
                )
        except subprocess.TimeoutExpired:
            logger.error("Ghostscript timed out after 120s")
            raise RuntimeError("PDF compression timed out")

        # Ghostscript may produce empty output for corrupted PDFs
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            logger.error("Ghostscript produced empty output, falling back to pypdf")
            return self._fallback_compress(output_path)

        logger.info(
            f"GS compressed PDF: {os.path.getsize(self.file_path)} -> "
            f"{os.path.getsize(output_path)} bytes"
        )
        return output_path

    def _fallback_compress(self, output_path: str) -> str:
        """Basic pypdf compression (content stream deflation only)."""
        reader = PdfReader(self.file_path)
        writer = PdfWriter()

        for page in reader.pages:
            page.compress_content_streams()
            writer.add_page(page)

        # Try to remove duplicated objects
        writer.remove_objects_from_page(writer.pages[0])  # no-op trigger

        with open(output_path, "wb") as f:
            writer.write(f)

        logger.info(
            f"Fallback compressed PDF: {os.path.getsize(self.file_path)} -> "
            f"{os.path.getsize(output_path)} bytes"
        )
        return output_path
