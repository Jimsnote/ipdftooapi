import os
import pathlib
import shutil
import subprocess
import tempfile
import threading
from app.core.logger import get_logger

logger = get_logger(__name__)

# LibreOffice headless 使用默认 user profile 时并发互踩（profile 锁），
# 第二个进程会失败或挂起——全局串行 + 每次转换独立 profile 双保险
_libreoffice_lock = threading.Lock()

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


def _run_libreoffice(cmd: list) -> subprocess.CompletedProcess:
    profile_dir = tempfile.mkdtemp(prefix="lo_profile_")
    profile_uri = pathlib.Path(profile_dir).as_uri()
    cmd.insert(1, f"-env:UserInstallation={profile_uri}")
    try:
        with _libreoffice_lock:
            result = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=120,
                check=True,
            )
        logger.info(f"LibreOffice stdout: {result.stdout.decode('utf-8', errors='ignore')[:200]}")
        return result
    except subprocess.TimeoutExpired:
        logger.error("LibreOffice conversion timed out after 120s")
        raise RuntimeError("Word 转 PDF 超时，请稍后重试")
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", errors="ignore") if e.stderr else ""
        logger.error(f"LibreOffice conversion failed: {stderr}")
        raise RuntimeError("Word 转 PDF 失败，请重试或更换文件")
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)


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

        _run_libreoffice(cmd)

        if not os.path.exists(expected_output):
            # LibreOffice might name it differently; search for any .pdf in outdir
            pdf_files = [f for f in os.listdir(output_dir) if f.endswith(".pdf")]
            if pdf_files:
                expected_output = os.path.join(output_dir, pdf_files[0])
            elif self.input_path.lower().endswith(".docx"):
                # 部分 OOXML 文件能被 python-docx 读取、却不能被 LibreOffice
                # 直接加载。先重存规范包，再仅对这类无产物任务重试一次。
                from docx import Document

                normalized_path = os.path.join(output_dir, f"{base_name}.normalized.docx")
                normalized_pdf = os.path.join(output_dir, f"{base_name}.normalized.pdf")
                try:
                    Document(self.input_path).save(normalized_path)
                    retry_cmd = [
                        libreoffice,
                        "--headless",
                        "--convert-to",
                        "pdf",
                        "--outdir",
                        output_dir,
                        normalized_path,
                    ]
                    _run_libreoffice(retry_cmd)
                    if os.path.exists(normalized_pdf):
                        expected_output = normalized_pdf
                finally:
                    if os.path.exists(normalized_path):
                        os.remove(normalized_path)
            if not os.path.exists(expected_output):
                raise RuntimeError("转换完成但未找到 PDF 输出文件")

        # Rename to desired output path if different
        if expected_output != os.path.abspath(output_path):
            shutil.move(expected_output, output_path)

        logger.info(f"Conversion completed: {output_path}")
        return output_path
