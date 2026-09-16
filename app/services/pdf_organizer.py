"""PDF 页面整理服务（重排 / 删除 / 复制 / 单页旋转）。

按前端传来的「输出页序」重建文档：每个输出页 = 原文档某页（可选叠加旋转角）。
- 重排 = 序列顺序即输出顺序
- 删除 = 原页码不出现在序列里
- 复制 = 同一原页码出现多次（insert_pdf 逐页拷贝天然支持）
- 旋转 = "N:angle" 把顺时针角度叠加到页面现有 /Rotate 上

语义说明（有意为之，与同类工具 organize 路线一致）：
- 新文档为重建产物，metadata 为空 —— 顺带清掉作者/软件等隐私字段
- 书签（outline）不保留；指向被删/被移动页面的跨页链接随之失效
- 附件（embedded files）不保留
"""

import os
from typing import List, Tuple

import fitz

from app.core.logger import get_logger

logger = get_logger(__name__)

VALID_ANGLES = (90, 180, 270)

# 输出页数上限：order 允许复制页码，没有上限时一次请求可撑出上千页
MAX_OUTPUT_PAGES = 1000


def normalize_rotation(angle: int) -> int:
    """把叠加后的角度归一化到 0-359（负角度 % 360 会保持负值，必须先加 360）。"""
    return ((angle % 360) + 360) % 360


def parse_order(order_str: str, total_pages: int) -> List[Tuple[int, int]]:
    """解析输出页序串（1 基）：每项为 "N"（原样保留）或 "N:angle"（叠加旋转）。

    示例："3,1,2" 重排；"1,3,5" 删除第 2/4 页；"2:90,1,2:90" 复制并旋转。
    返回 [(0 基原页码, 叠加角度), ...]，角度 ∈ {0, 90, 180, 270}。
    """
    entries: List[Tuple[int, int]] = []
    parts = [p.strip() for p in order_str.split(",") if p.strip()]
    if not parts:
        raise ValueError("页序不能为空")

    for part in parts:
        seg = part.split(":")
        if len(seg) > 2:
            raise ValueError(f"页序格式无效：{part}")
        try:
            page_num = int(seg[0])
        except ValueError:
            raise ValueError(f"页序格式无效：{part}")
        if not 1 <= page_num <= total_pages:
            raise ValueError(f"页码 {page_num} 超出范围（共 {total_pages} 页）")

        angle = 0
        if len(seg) == 2:
            try:
                angle = int(seg[1])
            except ValueError:
                raise ValueError(f"旋转角度无效：{part}")
            if angle not in VALID_ANGLES:
                raise ValueError(f"旋转角度只支持 90、180、270：{part}")

        entries.append((page_num - 1, angle))

    if len(entries) > MAX_OUTPUT_PAGES:
        raise ValueError(f"输出页数过多（最多 {MAX_OUTPUT_PAGES} 页），请分批处理")

    return entries


class PDFOrganizer:
    """Rebuild a PDF in a caller-defined page order via insert_pdf."""

    def __init__(self, input_path: str):
        self.input_path = input_path
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"Input file not found: {input_path}")
        self._ensure_not_encrypted()

    def _ensure_not_encrypted(self) -> None:
        """加密文件在 PyMuPDF 下 len() 能"成功"，但重建/保存会产出空内容或
        抛不可读错误——前置拦截，引导用户先解密。"""
        with fitz.open(self.input_path) as doc:
            if doc.needs_pass or doc.is_encrypted:
                raise ValueError("PDF 已加密，请先用「解除 PDF 密码」工具解密后再整理")

    @property
    def total_pages(self) -> int:
        with fitz.open(self.input_path) as doc:
            return len(doc)

    def organize(
        self,
        order: List[Tuple[int, int]],
        output_path: str,
    ) -> Tuple[int, int]:
        """Rebuild the document following `order` and save.

        Args:
            order: [(0 基原页码, 叠加角度), ...]，由 parse_order 产出。
        Returns:
            (原总页数, 输出总页数)
        """
        if not order:
            raise ValueError("页序不能为空")
        if os.path.abspath(output_path) == os.path.abspath(self.input_path):
            raise ValueError("输出文件不能与输入文件相同")

        src = fitz.open(self.input_path)
        try:
            out = fitz.open()
            try:
                for index, angle in order:
                    if not 0 <= index < len(src):
                        raise ValueError(f"页码 {index + 1} 超出范围（共 {len(src)} 页）")
                    out.insert_pdf(src, from_page=index, to_page=index)
                    if angle:
                        page = out[-1]
                        page.set_rotation(normalize_rotation(page.rotation + angle))

                out.save(output_path, garbage=3, deflate=True)
                logger.info(
                    f"Organized {len(src)} page(s) into {len(out)} page(s): "
                    f"{self.input_path} -> {output_path}"
                )
                return len(src), len(out)
            finally:
                out.close()
        finally:
            src.close()
