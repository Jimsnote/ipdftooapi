import os
import tempfile

import pytest
from pypdf import PdfReader, PdfWriter

from app.services.pdf_splitter import PDFSplitter


def _make_pdf(path: str, num_pages: int) -> None:
    writer = PdfWriter()
    for _ in range(num_pages):
        # pypdf 4.0.0 在 PDF 尚无任何页时要求显式尺寸，否则抛 PageSizeNotDefinedError
        writer.add_blank_page(width=595.27, height=841.89)
    with open(path, "wb") as f:
        writer.write(f)


def _split(mode: str, value: str, num_pages: int):
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src.pdf")
    _make_pdf(src, num_pages)
    out = os.path.join(d, "out")
    os.makedirs(out)
    return PDFSplitter(src).split(mode=mode, value=value, output_dir=out), out


def test_split_all_pages():
    paths, _ = _split("all", "", 3)
    assert len(paths) == 3
    assert all(len(PdfReader(p).pages) == 1 for p in paths)


def test_split_ranges():
    paths, _ = _split("ranges", "1-3", 5)
    assert len(paths) == 1
    assert len(PdfReader(paths[0]).pages) == 3


def test_split_ranges_single_page():
    paths, _ = _split("ranges", "4", 5)
    assert len(paths) == 1
    assert len(PdfReader(paths[0]).pages) == 1


def test_split_fixed():
    paths, _ = _split("fixed", "2", 5)
    # 5 页，每 2 页一份 -> 2,2,1 共 3 份
    assert len(paths) == 3
    counts = [len(PdfReader(p).pages) for p in paths]
    assert counts == [2, 2, 1]


def test_unknown_mode_raises():
    with pytest.raises(ValueError):
        _split("bogus", "", 2)
