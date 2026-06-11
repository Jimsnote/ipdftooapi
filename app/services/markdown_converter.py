import os
from markitdown import MarkItDown

from app.core.logger import get_logger

logger = get_logger(__name__)


class OfficeToMarkdownConverter:
    """使用微软 MarkItDown 将 Office 文档转换为 Markdown。"""

    def __init__(self):
        self.md = MarkItDown()

    def convert(self, input_path: str, output_path: str) -> dict:
        """
        将 Office 文档转换为 Markdown 文件。

        Args:
            input_path: 输入文件路径（.docx/.pptx/.xlsx 等）
            output_path: 输出 .md 文件路径

        Returns:
            {"char_count": int}
        """
        try:
            result = self.md.convert(input_path)
            text_content = result.text_content or ""

            with open(output_path, "w", encoding="utf-8") as f:
                f.write(text_content)

            char_count = len(text_content)
            logger.info(f"MarkItDown converted {input_path} -> {output_path}, {char_count} chars")
            return {"char_count": char_count}
        except Exception as e:
            logger.error(f"MarkItDown conversion failed for {input_path}: {e}")
            raise
