import os
import shutil
import subprocess

import pytest
from pypdf import PdfWriter

from app.services.pdf_compressor import GS_TIMEOUT_SECONDS, PDFCompressor


def _write_pdf(path: str) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=595.27, height=841.89)
    with open(path, "wb") as f:
        writer.write(f)


def test_gs_uses_timeout_below_proxy_limit(tmp_path, monkeypatch):
    src = str(tmp_path / "input.pdf")
    out = str(tmp_path / "compressed.pdf")
    _write_pdf(src)
    compressor = PDFCompressor(src)
    monkeypatch.setattr(compressor, "_find_gs", lambda: "gs")

    def fake_run(cmd, capture_output, text, timeout):
        assert timeout == GS_TIMEOUT_SECONDS
        assert timeout < 300
        shutil.copyfile(src, out)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    compressor.compress(out, level="normal")
    assert os.path.exists(out)


@pytest.mark.parametrize("failure", ["timeout", "nonzero"])
def test_gs_failure_removes_partial_output(tmp_path, monkeypatch, failure):
    src = str(tmp_path / "input.pdf")
    out = str(tmp_path / "compressed.pdf")
    _write_pdf(src)
    compressor = PDFCompressor(src)
    monkeypatch.setattr(compressor, "_find_gs", lambda: "gs")

    def fake_run(cmd, capture_output, text, timeout):
        with open(out, "wb") as f:
            f.write(b"partial PDF")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(cmd, timeout)
        return subprocess.CompletedProcess(cmd, 1, "", "bad PDF")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(RuntimeError):
        compressor.compress(out, level="normal")
    assert not os.path.exists(out)
