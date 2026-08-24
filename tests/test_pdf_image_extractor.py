"""PDFImageExtractor 纯逻辑测试：真实构造含内嵌图片的 PDF 验证提取。

覆盖点：
- PNG/JPEG 原图无损提取（字节级一致）；
- 同一图片被多页引用时按 xref 去重只导出一份；
- 无图 PDF 返回空列表；
- ZIP 打包。
"""

import io
import os
import tempfile
import zipfile

import fitz  # PyMuPDF
from PIL import Image

from app.services.pdf_image_extractor import PDFImageExtractor


def _png_bytes(color=(200, 30, 30), size=(60, 40)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _jpg_bytes(color=(30, 30, 200), size=(50, 50)) -> bytes:
    img = Image.new("RGB", size, color)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=95)
    return buf.getvalue()


def _make_pdf_with_images(path: str) -> None:
    """2 页 PDF：第 1 页一张 PNG + 一张 JPEG，第 2 页再次引用同一张 PNG。"""
    doc = fitz.open()
    png = _png_bytes()
    jpg = _jpg_bytes()

    page1 = doc.new_page(width=595, height=842)
    page1.insert_image(fitz.Rect(50, 50, 200, 200), stream=png)
    page1.insert_image(fitz.Rect(50, 300, 200, 400), stream=jpg)

    page2 = doc.new_page(width=595, height=842)
    page2.insert_image(fitz.Rect(100, 100, 250, 250), stream=png)  # 同图复用

    doc.save(path)
    doc.close()


def _extract():
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src.pdf")
    _make_pdf_with_images(src)
    out = os.path.join(d, "out")
    return PDFImageExtractor(src).extract(out), out


def test_extract_all_unique_images():
    images, _ = _extract()
    # PNG 复用应去重：共 2 张（1 PNG + 1 JPG），而非 3 张
    assert len(images) == 2
    exts = sorted(os.path.splitext(img["filename"])[1] for img in images)
    assert exts == [".jpg", ".png"]


def test_extract_lossless_original_bytes():
    images, out = _extract()
    jpg = next(img for img in images if img["filename"].endswith(".jpg"))
    # 原始字节无损：文件内容应与嵌入的 JPEG 字节完全一致
    with open(jpg["path"], "rb") as f:
        assert f.read() == _jpg_bytes()
    # 元数据齐全
    assert jpg["width"] == 50 and jpg["height"] == 50
    assert jpg["size"] > 0
    assert os.path.dirname(jpg["path"]) == out


def test_extract_empty_pdf_returns_empty():
    d = tempfile.mkdtemp()
    src = os.path.join(d, "blank.pdf")
    doc = fitz.open()
    doc.new_page(width=595, height=842)
    doc.save(src)
    doc.close()
    assert PDFImageExtractor(src).extract(os.path.join(d, "out")) == []


def test_zip_images():
    images, out = _extract()
    zip_path = os.path.join(out, "extracted-images.zip")
    PDFImageExtractor.zip_images(images, zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        names = sorted(zf.namelist())
        assert len(names) == 2
        assert all("/" not in n for n in names)  # 平铺无目录
