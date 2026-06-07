import os
from typing import Optional

import pymupdf4llm

from app.core.logger import get_logger

logger = get_logger(__name__)


class PDFToMarkdownConverter:
    """Convert PDF files to Markdown using PyMuPDF4LLM.

    Lightweight, rule-based extraction. No neural network required.
    Suitable for standard electronic PDFs with clear text layout.
    """

    def convert(
        self,
        input_path: str,
        output_path: str,
        pages: Optional[str] = None,
    ) -> dict:
        """Convert PDF to Markdown.

        Args:
            input_path: Path to input PDF file.
            output_path: Path to write output .md file.
            pages: Optional page range, e.g. "1-5,8,10" or "1-10".
                   If None, convert all pages.

        Returns:
            dict with page_count, char_count, and preview text.
        """
        kwargs = {}
        if pages:
            kwargs["pages"] = self._parse_pages(pages)

        # Convert to markdown string
        md_text = pymupdf4llm.to_markdown(input_path, **kwargs)

        # Write output
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(md_text)

        page_count = len(md_text.split("\n\n")) if isinstance(md_text, str) else len(md_text)
        char_count = len(md_text) if isinstance(md_text, str) else sum(len(c["text"]) for c in md_text)

        logger.info(
            f"Converted PDF to Markdown: {input_path} -> {output_path} "
            f"({char_count} chars)"
        )

        return {
            "page_count": page_count,
            "char_count": char_count,
            "preview": (md_text[:500] + "...") if isinstance(md_text, str) and len(md_text) > 500 else md_text,
        }

    @staticmethod
    def _parse_pages(pages_str: str) -> list:
        """Parse page range string to list of 0-based page numbers.

        Examples:
            "1-3,5,7-10" -> [0,1,2,4,6,7,8,9]
            "all" -> None (handled by caller)
        """
        if pages_str.lower() == "all":
            return None

        result = []
        parts = [p.strip() for p in pages_str.split(",")]
        for part in parts:
            if "-" in part:
                start, end = part.split("-")
                # Convert to 0-based
                result.extend(range(int(start) - 1, int(end)))
            else:
                result.append(int(part) - 1)
        return result
