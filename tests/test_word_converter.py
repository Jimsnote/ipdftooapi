import os

from docx import Document

from app.services import word_converter


def test_docx_resave_retry_when_libreoffice_produces_no_output(tmp_path, monkeypatch):
    input_path = tmp_path / "input.docx"
    output_path = tmp_path / "converted.pdf"
    doc = Document()
    doc.add_paragraph("normalization test")
    doc.save(str(input_path))
    monkeypatch.setattr(word_converter, "_find_libreoffice", lambda: "/bin/fake")
    calls = []

    def fake_run(cmd):
        calls.append(cmd[-1])
        if cmd[-1].endswith(".normalized.docx"):
            normalized_pdf = tmp_path / "input.normalized.pdf"
            normalized_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")

    monkeypatch.setattr(word_converter, "_run_libreoffice", fake_run)
    result = word_converter.WordConverter(str(input_path)).convert(str(output_path))

    assert result == str(output_path)
    assert output_path.read_bytes().startswith(b"%PDF-")
    assert calls == [str(input_path), str(tmp_path / "input.normalized.docx")]
    assert not (tmp_path / "input.normalized.docx").exists()
