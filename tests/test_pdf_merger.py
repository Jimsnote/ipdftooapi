import os
import tempfile

import pytest
from pypdf import PdfReader, PdfWriter

from app.services.pdf_merger import PDFMerger


def _make_pdf(path: str, num_pages: int) -> None:
    writer = PdfWriter()
    for _ in range(num_pages):
        # pypdf 4.0.0 在 PDF 尚无任何页时要求显式尺寸，否则抛 PageSizeNotDefinedError
        writer.add_blank_page(width=595.27, height=841.89)
    with open(path, "wb") as f:
        writer.write(f)


def test_merge_basic():
    d = tempfile.mkdtemp()
    p1 = os.path.join(d, "a.pdf")
    p2 = os.path.join(d, "b.pdf")
    _make_pdf(p1, 2)
    _make_pdf(p2, 3)
    out = os.path.join(d, "merged.pdf")

    PDFMerger([p1, p2]).merge(out)

    assert os.path.exists(out)
    assert len(PdfReader(out).pages) == 5


def test_merge_empty_raises():
    with pytest.raises(ValueError):
        PDFMerger([]).merge(os.path.join(tempfile.mkdtemp(), "x.pdf"))


def test_merge_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        PDFMerger(["/this/does/not/exist.pdf"]).merge(
            os.path.join(tempfile.mkdtemp(), "x.pdf")
        )
