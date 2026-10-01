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
            raise ValueError(f"不支持的压缩级别：{level}（可选：extreme / normal / light）")

        # 部署冒烟新发现（#19 关联）：生产 Ghostscript 10.x 对加密 PDF 返回码为 0
        # 且仍写出空白壳 PDF（"No pages will be processed"），用户会静默拿到废文件；
        # pypdf fallback 则抛 FileNotDecryptedError。统一在入口拦截，给 400 中文引导。
        from pypdf import PdfReader

        reader = PdfReader(self.file_path)
        if reader.is_encrypted:
            raise ValueError("PDF 已加密，请先用「解除 PDF 密码」工具解密后再压缩")
        # 审计 L11：0 页 PDF 压缩会产出空文件，前置拦截
        if len(reader.pages) == 0:
            raise ValueError("PDF 没有任何页面，无法压缩")

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

        # 审计 L30：gs 路径与 fallback 同规则——输出 ≥ 输入时回传原文件，
        # 路由按实际体积给出如实文案（不再"压缩"出更大的文件还报完成）
        in_size = os.path.getsize(self.file_path)
        out_size = os.path.getsize(output_path)
        if out_size >= in_size:
            logger.info(
                f"GS compression ineffective ({in_size} -> {out_size} bytes), "
                "returning original file content"
            )
            shutil.copyfile(self.file_path, output_path)
        else:
            logger.info(
                f"GS compressed PDF: {in_size} -> {out_size} bytes"
            )
        return output_path

    def _fallback_compress(self, output_path: str) -> str:
        """Basic pypdf compression (content stream deflation only)."""
        reader = PdfReader(self.file_path)
        writer = PdfWriter()

        for page in reader.pages:
            writer.add_page(page)

        for page in writer.pages:
            page.compress_content_streams()

        with open(output_path, "wb") as f:
            writer.write(f)

        # 审计 #19：fallback 产出 ≥ 输入时回传原文件内容，路由按实际体积给出
        # 如实的结果文案（reduction=0），不再越压越大还报"压缩完成"
        in_size = os.path.getsize(self.file_path)
        out_size = os.path.getsize(output_path)
        if out_size >= in_size:
            logger.info(
                f"Fallback compression ineffective ({in_size} -> {out_size} bytes), "
                "returning original file content"
            )
            shutil.copyfile(self.file_path, output_path)
        else:
            logger.info(f"Fallback compressed PDF: {in_size} -> {out_size} bytes")
        return output_path
