import os
import zipfile
from typing import List, Dict

import fitz  # PyMuPDF

from app.core.logger import get_logger

logger = get_logger(__name__)

# 单个 PDF 可提取的图片数安全上限（防异常文档撑爆磁盘）
MAX_IMAGES = 500

# 浏览器/看图软件可直接打开的格式 —— 这些保留原始字节（真无损）；
# 其余（JBIG2/JPEG2000 等扫描件常见编码）统一转 PNG
_RAW_KEEP_EXTS = {"png", "jpeg"}


class PDFImageExtractor:
    """提取 PDF 中的内嵌图片。

    与 PDFToImageConverter（整页渲染为图片）不同：这里抽取的是文档里
    嵌入的原始图片对象（照片/扫描图/Logo），不经重采样，无质量损失。

    处理策略：
    - 按 xref 去重：同一图片被多页引用时只导出一份（按首次出现页命名）；
    - 带 SMASK 透明通道的图片：基图 + 蒙版复合后输出 PNG，保留透明背景；
    - PNG/JPEG 直接写原始字节（无损）；JBIG2/JPX 等格式转 PNG 以保证通用性。
    """

    def __init__(self, input_path: str):
        self.input_path = input_path
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"Input file not found: {input_path}")

    def extract(self, output_dir: str) -> List[Dict]:
        """提取全部内嵌图片到 output_dir，返回图片信息列表（按页面顺序）。"""
        os.makedirs(output_dir, exist_ok=True)
        results: List[Dict] = []
        seen_xrefs = set()

        doc = fitz.open(self.input_path)
        try:
            for page_index in range(len(doc)):
                page = doc.load_page(page_index)
                for img in page.get_images(full=True):
                    xref, smask = img[0], img[1]
                    if xref in seen_xrefs:
                        continue
                    seen_xrefs.add(xref)
                    try:
                        results.append(
                            self._save_one(doc, xref, smask, page_index + 1, len(results) + 1, output_dir)
                        )
                    except Exception as e:
                        logger.warning(f"Skip image xref={xref} on page {page_index + 1}: {e}")
                    if len(results) >= MAX_IMAGES:
                        logger.warning(f"Image count hit safety cap {MAX_IMAGES}, stop extracting")
                        return results
        finally:
            doc.close()

        logger.info(f"Extracted {len(results)} embedded image(s) from {self.input_path}")
        return results

    def _save_one(
        self, doc: fitz.Document, xref: int, smask: int, page_no: int, seq: int, output_dir: str
    ) -> Dict:
        base_name = f"p{page_no:03d}_img{seq:02d}"

        if smask > 0:
            # 基图 + 透明蒙版复合为 PNG
            base = fitz.Pixmap(doc, xref)
            if base.colorspace and base.colorspace.n > 3:
                base = fitz.Pixmap(fitz.csRGB, base)  # CMYK → RGB
            mask = fitz.Pixmap(doc, smask)
            pix = fitz.Pixmap(base, mask)
            filename = f"{base_name}.png"
            path = os.path.join(output_dir, filename)
            pix.save(path)
            width, height = pix.width, pix.height
        else:
            info = doc.extract_image(xref)
            ext = info["ext"].lower()
            width, height = info.get("width", 0), info.get("height", 0)
            if ext in _RAW_KEEP_EXTS:
                # 原始字节无损写出（jpeg 扩展名规范化为 jpg）
                filename = f"{base_name}.{'jpg' if ext == 'jpeg' else 'png'}"
                path = os.path.join(output_dir, filename)
                with open(path, "wb") as f:
                    f.write(info["image"])
            else:
                # JBIG2/JPEG2000 等 → PNG
                pix = fitz.Pixmap(doc, xref)
                if pix.colorspace and pix.colorspace.n > 3:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                filename = f"{base_name}.png"
                path = os.path.join(output_dir, filename)
                pix.save(path)

        return {
            "filename": filename,
            "path": path,
            "page": page_no,
            "width": width,
            "height": height,
            "size": os.path.getsize(path),
        }

    @staticmethod
    def zip_images(images: List[Dict], zip_path: str) -> str:
        """把提取出的图片打包为 ZIP（内部文件名用展示名，不带目录）。"""
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for img in images:
                zf.write(img["path"], img["filename"])
        logger.info(f"Created ZIP archive: {zip_path} ({len(images)} files)")
        return zip_path
