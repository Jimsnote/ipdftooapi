import os
from typing import List
from pypdf import PdfReader, PdfWriter

from app.core.logger import get_logger

logger = get_logger(__name__)

# 审计 #7：ranges 模式护栏（实测 1MB value 600s 内写出 25 万+ 文件耗尽 inode）
MAX_RANGE_PARTS = 200        # 最多区间数
MAX_RANGE_VALUE_LEN = 4096   # value 字符串长度上限


class PDFSplitter:
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.reader = PdfReader(file_path)
        self.total_pages = len(self.reader.pages)

    def split(
        self, mode: str = "all", value: str = "", output_dir: str = "."
    ) -> List[str]:
        if self.total_pages == 0:
            raise ValueError("PDF 没有任何页面")

        if mode == "all":
            return self._split_all(output_dir)
        elif mode == "ranges":
            return self._split_ranges(value, output_dir)
        elif mode == "fixed":
            try:
                pages_per_file = int(value) if value else 1
            except ValueError:
                raise ValueError("每个文件页数需为正整数，如：2")
            if pages_per_file < 1:
                raise ValueError("每个文件页数需为正整数，如：2")
            return self._split_fixed(pages_per_file, output_dir)
        else:
            raise ValueError(f"不支持的拆分模式：{mode}")

    def _split_all(self, output_dir: str) -> List[str]:
        """Extract each page as a separate PDF."""
        output_paths = []
        for i, page in enumerate(self.reader.pages):
            writer = PdfWriter()
            writer.add_page(page)
            path = os.path.join(output_dir, f"page_{i + 1}.pdf")
            with open(path, "wb") as f:
                writer.write(f)
            output_paths.append(path)
        logger.info(f"Split into {len(output_paths)} single-page PDFs")
        return output_paths

    def _split_ranges(self, ranges_str: str, output_dir: str) -> List[str]:
        """Split by page ranges like '1-3,5-10'."""
        # 审计 #7：区间数/长度护栏，超限 400（此前无上限可单请求写 25 万+ 文件）
        if len(ranges_str) > MAX_RANGE_VALUE_LEN:
            raise ValueError("拆分区间内容过长（最多 4096 字符），请精简后重试")
        output_paths = []
        parts = [p.strip() for p in ranges_str.split(",") if p.strip()]
        if len(parts) > MAX_RANGE_PARTS:
            raise ValueError(
                f"拆分区间过多（{len(parts)} 个 > 上限 {MAX_RANGE_PARTS} 个），请减少区间后重试"
            )

        for idx, part in enumerate(parts):
            if "-" in part:
                start, end = part.split("-")
                try:
                    start_num = int(start)
                    end_num = int(end)
                except ValueError:
                    raise ValueError(f"页码区间格式无效：{part}")
                if start_num > end_num:
                    raise ValueError(f"页码区间无效：{part}")
                start_page = max(0, start_num - 1)
                end_page = min(self.total_pages, end_num)
            else:
                try:
                    start_num = int(part)
                except ValueError:
                    raise ValueError(f"页码格式无效：{part}")
                start_page = start_num - 1
                end_page = start_page + 1

            # 区间被夹取后为空（如 "5-2"、"999" 单页越界、整体超出文档页数）
            # 时不产出空 PDF
            if (
                start_page >= end_page
                or start_page >= self.total_pages
                or end_page <= 0
            ):
                raise ValueError(f"页码区间 {part} 超出文档页数范围（共 {self.total_pages} 页）")

            writer = PdfWriter()
            for i in range(start_page, end_page):
                if 0 <= i < self.total_pages:
                    writer.add_page(self.reader.pages[i])

            path = os.path.join(output_dir, f"part_{idx + 1}.pdf")
            with open(path, "wb") as f:
                writer.write(f)
            output_paths.append(path)

        logger.info(f"Split into {len(output_paths)} range-based PDFs")
        return output_paths

    def _split_fixed(self, pages_per_file: int, output_dir: str) -> List[str]:
        """Split into files with fixed number of pages."""
        output_paths = []
        current_writer = PdfWriter()
        file_count = 0

        for i, page in enumerate(self.reader.pages):
            current_writer.add_page(page)
            if (i + 1) % pages_per_file == 0 or i == self.total_pages - 1:
                file_count += 1
                path = os.path.join(output_dir, f"part_{file_count}.pdf")
                with open(path, "wb") as f:
                    current_writer.write(f)
                output_paths.append(path)
                current_writer = PdfWriter()

        logger.info(f"Split into {len(output_paths)} fixed-size PDFs")
        return output_paths
