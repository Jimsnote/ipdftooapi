import os
import shutil

from pypdf import PdfReader, PdfWriter
from pypdf.errors import FileNotDecryptedError

from app.core.logger import get_logger

logger = get_logger(__name__)


class PDFUnlocker:
    """解除 PDF 密码保护（pypdf decrypt + 重写不加密副本）。

    两类情况：
    - 仅权限密码（能打开但限制打印/复制）：打开密码为空，decrypt("") 即可解除；
    - 打开密码：需要用户提供正确密码，解密后重写为不加密副本。

    不做的事：不知道打开密码的暴力破解（主流在线工具同样不做）。
    """

    def __init__(self, input_path: str):
        self.input_path = input_path
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"Input file not found: {input_path}")

    def unlock(self, output_path: str, password: str = "") -> dict:
        """解除保护。返回 {"was_encrypted": bool}；密码错误抛 ValueError。"""
        try:
            reader = PdfReader(self.input_path)
        except Exception as e:
            raise ValueError(f"无法读取 PDF 文件（文件可能已损坏）: {e}")

        if not reader.is_encrypted:
            # 未加密：直接复制输出，告知无需解除
            shutil.copyfile(self.input_path, output_path)
            logger.info(f"PDF not encrypted, copied as-is: {output_path}")
            return {"was_encrypted": False}

        # pypdf decrypt 返回 0=失败 / 1=用户密码 / 2=所有者密码
        result = reader.decrypt(password)
        if result == 0:
            raise ValueError("密码不正确，请确认后重试（该文件打开时需要密码）")

        try:
            writer = PdfWriter()
            writer.append(reader)
            with open(output_path, "wb") as f:
                writer.write(f)
        except FileNotDecryptedError:
            raise ValueError("密码不正确，请确认后重试（该文件打开时需要密码）")

        logger.info(f"PDF unlocked: {self.input_path} -> {output_path}")
        return {"was_encrypted": True}
