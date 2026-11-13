import os
import tempfile

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, NameObject, NullObject, NumberObject

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


@pytest.mark.parametrize("indirect_annots", [False, True])
def test_merge_link_action_without_s_is_ignored(indirect_annots):
    d = tempfile.mkdtemp()
    path = os.path.join(d, "invalid-link.pdf")
    writer = PdfWriter()
    page = writer.add_blank_page(width=595.27, height=841.89)
    action = DictionaryObject()
    action[NameObject("/Filter")] = NameObject("/FlateDecode")
    annotation = DictionaryObject()
    annotation[NameObject("/Type")] = NameObject("/Annot")
    annotation[NameObject("/Subtype")] = NameObject("/Link")
    annotation[NameObject("/Rect")] = ArrayObject(
        [NumberObject(10), NumberObject(10), NumberObject(100), NumberObject(100)]
    )
    annotation[NameObject("/A")] = action
    annots = ArrayObject([writer._add_object(annotation)])
    page[NameObject("/Annots")] = writer._add_object(annots) if indirect_annots else annots
    with open(path, "wb") as f:
        writer.write(f)

    out = os.path.join(d, "merged.pdf")
    PDFMerger([path]).merge(out)

    merged_page = PdfReader(out).pages[0]
    assert len(merged_page["/Annots"]) == 1
    assert "/A" not in merged_page["/Annots"][0].get_object()


def test_merge_null_annotations_does_not_fail_in_cleanup():
    d = tempfile.mkdtemp()
    path = os.path.join(d, "null-annots.pdf")
    writer = PdfWriter()
    page = writer.add_blank_page(width=595.27, height=841.89)
    page[NameObject("/Annots")] = writer._add_object(NullObject())
    with open(path, "wb") as f:
        writer.write(f)

    out = os.path.join(d, "merged.pdf")
    PDFMerger([path]).merge(out)

    assert len(PdfReader(out).pages) == 1
