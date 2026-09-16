"""PDF 页面旋转服务。

用 PyMuPDF 的 /Rotate 页面属性实现（不重排内容流，无损、秒级）：
- 在页面现有旋转角基础上叠加（与 coolpdf/Adobe 语义一致）
- 归一化到 0/90/180/270，防脏文件携带负角度导致 % 360 出负值
"""

import os
from typing import List, Optional, Set, Tuple

import fitz

from app.core.logger import get_logger

logger = get_logger(__name__)

VALID_ANGLES = (90, 180, 270)


def normalize_rotation(angle: int) -> int:
    """把叠加后的角度归一化到 0-359（负角度 % 360 会保持负值，必须先加 360）。"""
    return ((angle % 360) + 360) % 360


class PDFRotator:
    """Rotate pages of a PDF file via the /Rotate attribute."""

    def __init__(self, input_path: str):
        self.input_path = input_path
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"Input file not found: {input_path}")
        with fitz.open(input_path) as doc:
            if doc.needs_pass or doc.is_encrypted:
                raise ValueError("PDF 已加密，请先用「解除 PDF 密码」工具解密后再旋转")

    @property
    def total_pages(self) -> int:
        with fitz.open(self.input_path) as doc:
            return len(doc)

    def rotate(
        self,
        angle: int,
        output_path: str,
        pages: Optional[Set[int]] = None,
    ) -> Tuple[int, int]:
        """Rotate pages clockwise by `angle` degrees and save.

        Args:
            angle: 90 / 180 / 270（顺时针）。
            pages: 要旋转的 1 基页码集合；None = 全部页面。
        Returns:
            (总页数, 实际旋转页数)
        """
        if angle not in VALID_ANGLES:
            raise ValueError("旋转角度只支持 90、180、270")
        if os.path.abspath(output_path) == os.path.abspath(self.input_path):
            # PyMuPDF save-to-original 会抛"must be incremental"，
            # 这里提前给出可读错误（生产链路输入/输出永远不同名）
            raise ValueError("输出文件不能与输入文件相同")

        doc = fitz.open(self.input_path)
        try:
            targets: List[int]
            if pages is None:
                targets = list(range(len(doc)))
            else:
                targets = sorted(p - 1 for p in pages)  # 转 0 基
                targets = [i for i in targets if 0 <= i < len(doc)]
                if not targets:
                    raise ValueError("未指定有效的旋转页码")

            for index in targets:
                page = doc[index]
                current = page.rotation
                page.set_rotation(normalize_rotation(current + angle))

            doc.save(output_path, garbage=3, deflate=True)
            rotated = len(targets)
            logger.info(
                f"Rotated {rotated}/{len(doc)} page(s) by {angle} degrees: "
                f"{self.input_path} -> {output_path}"
            )
            return len(doc), rotated
        finally:
            doc.close()

    @staticmethod
    def parse_page_list(pages_str: str, total_pages: int) -> Set[int]:
        """解析页码串（1 基）："1,3,5"、"2-4"、"1,3-5,7"。

        与 PDFPageRemover.parse_page_list 同语义；返回空集合表示无有效页码。
        """
        result: Set[int] = set()
        parts = [p.strip() for p in pages_str.split(",") if p.strip()]

        for part in parts:
            if "-" in part:
                start, end = part.split("-", 1)
                start_num = int(start.strip())
                end_num = int(end.strip())
                if start_num > end_num:
                    raise ValueError(f"页码区间无效：{part}")
                # 区间先夹到 [1, total_pages] 再迭代：防 "1-99999999"
                # 这类超大区间把循环撑到上亿次（DoS）
                for p in range(max(1, start_num), min(end_num, total_pages) + 1):
                    result.add(p)
            else:
                p = int(part.strip())
                if 1 <= p <= total_pages:
                    result.add(p)

        return result
