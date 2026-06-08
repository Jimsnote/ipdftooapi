import os
import shutil
import subprocess
import tempfile
from app.core.logger import get_logger

logger = get_logger(__name__)

# LibreOffice binary paths to try (in order of preference)
_LIBREOFFICE_BINARIES = [
    "libreoffice",
    "/usr/bin/libreoffice",
    "/usr/lib/libreoffice/program/soffice",
]


def _find_libreoffice() -> str:
    """Find available LibreOffice binary."""
    for binary in _LIBREOFFICE_BINARIES:
        if shutil.which(binary):
            return binary
    raise RuntimeError(
        "LibreOffice not found. Please install it: sudo apt install -y libreoffice"
    )


class WordConverter:
    """Convert Word documents (.docx, .doc) to PDF using LibreOffice."""

    def __init__(self, input_path: str):
        self.input_path = input_path
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"Input file not found: {input_path}")

    def convert(self, output_path: str) -> str:
        """
        Convert the Word document to PDF.

        Args:
            output_path: Path where the PDF should be saved.

        Returns:
            Absolute path to the generated PDF.
        """
        libreoffice = _find_libreoffice()
        output_dir = os.path.dirname(os.path.abspath(output_path))
        os.makedirs(output_dir, exist_ok=True)

        # LibreOffice --convert-to outputs to the specified --outdir,
        # with the same base filename but .pdf extension
        input_basename = os.path.basename(self.input_path)
        base_name = os.path.splitext(input_basename)[0]
        expected_output = os.path.join(output_dir, f"{base_name}.pdf")

        # Remove existing output to avoid conflicts
        if os.path.exists(expected_output):
            os.remove(expected_output)

        cmd = [
            libreoffice,
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            output_dir,
            self.input_path,
        ]

        logger.info(f"Converting Word to PDF: {self.input_path}")

        try:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,  # 2 minutes max
                check=True,
            )
            logger.info(f"LibreOffice stdout: {result.stdout.decode('utf-8', errors='ignore')[:200]}")
        except subprocess.TimeoutExpired:
            logger.error("LibreOffice conversion timed out after 120s")
            raise RuntimeError("Word 转 PDF 超时，请稍后重试")
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode("utf-8", errors="ignore") if e.stderr else ""
            logger.error(f"LibreOffice conversion failed: {stderr}")
            raise RuntimeError(f"Word 转 PDF 失败: {stderr}")

        if not os.path.exists(expected_output):
            # LibreOffice might name it differently; search for any .pdf in outdir
            pdf_files = [f for f in os.listdir(output_dir) if f.endswith(".pdf")]
            if pdf_files:
                expected_output = os.path.join(output_dir, pdf_files[0])
            else:
                raise RuntimeError("转换完成但未找到 PDF 输出文件")

        # Rename to desired output path if different
        if expected_output != os.path.abspath(output_path):
            shutil.move(expected_output, output_path)

        logger.info(f"Conversion completed: {output_path}")
        return output_path
