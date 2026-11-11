from types import SimpleNamespace

from app.services.pdf_to_word import PDFToWordConverter  # noqa: F401
from pdf2docx.page.RawPageFitz import RawPageFitz


def test_zero_area_text_span_is_skipped_without_division_error():
    zero_area = {"bbox": (10, 10, 10, 30), "font": "TestFont", "text": "x"}
    raw = {"blocks": [{"lines": [{"spans": [zero_area]}]}]}

    class FakeEngine:
        def get_text(self, *args, **kwargs):
            return raw

        def get_texttrace(self):
            return [{"type": 3, "bbox": zero_area["bbox"], "font": "TestFont"}]

    page = SimpleNamespace(page_engine=FakeEngine())
    blocks = RawPageFitz._preprocess_text(page, ocr=0)
    assert blocks == raw["blocks"]
