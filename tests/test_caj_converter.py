"""CAJ 转 PDF 服务测试（docs/CAJ_TO_PDF_DESIGN.md）。

- detect_caj_type：六类文件头识别（合成 + 真实样本）
- pdf-embedded 端到端：真实样本（本地 C:/home/icreate/test/caj）转换产物校验
- C8/HN 路径依赖服务器环境（mutool + libjbigdec.so），本地 skip，部署后回归
"""

import os

import pytest

from app.services.caj_converter import detect_caj_type, convert_caj_to_pdf

SAMPLE_DIR = r"C:\home\icreate\test\caj"


def _make(path, head: bytes):
    with open(path, "wb") as f:
        f.write(head + b"\x00" * 100)
    return path


class TestDetectCajType:
    def test_pdf_embedded(self, tmp_path):
        p = _make(str(tmp_path / "a.caj"), b"%PDF-1.7\n%%EOF")
        assert detect_caj_type(p) == "pdf-embedded"

    def test_c8(self, tmp_path):
        p = _make(str(tmp_path / "b.caj"), b"\xc8\x00\x00\x00\x00\x00\x00\x00")
        assert detect_caj_type(p) == "C8"

    def test_caj(self, tmp_path):
        p = _make(str(tmp_path / "c.caj"), b"CAJ\xff\xfe" + b"\x00" * 50)
        assert detect_caj_type(p) == "CAJ"

    def test_hn(self, tmp_path):
        p = _make(str(tmp_path / "d.caj"), b"HN\x00\x01" + b"\x00" * 50)
        assert detect_caj_type(p) == "HN"

    def test_kdh(self, tmp_path):
        p = _make(str(tmp_path / "e.caj"), b"KDH\x00\x02" + b"\x00" * 50)
        assert detect_caj_type(p) == "KDH"

    def test_unknown(self, tmp_path):
        p = _make(str(tmp_path / "f.caj"), b"XYZ" + b"\x00" * 60)
        assert detect_caj_type(p) == "unknown"

    def test_real_samples(self):
        """真实样本分类（本机有样本时）。"""
        if not os.path.isdir(SAMPLE_DIR):
            pytest.skip("本机无 CAJ 样本")
        cais = [
            f for f in os.listdir(SAMPLE_DIR)
            if f.endswith(".caj")
        ]
        assert len(cais) >= 5
        results = {
            f: detect_caj_type(os.path.join(SAMPLE_DIR, f)) for f in cais
        }
        types = set(results.values())
        # 全部样本必须被识别（无 unknown）
        assert "unknown" not in types, results
        # 实测构成：3 个 pdf-embedded + 2 个 C8（共 5 个）
        assert types <= {"pdf-embedded", "C8"}


class TestConvertPdfEmbedded:
    def test_real_sample_end_to_end(self, tmp_path):
        """真实伪后缀样本端到端：输出必须为有效可搜索 PDF。"""
        src = None
        if os.path.isdir(SAMPLE_DIR):
            for f in sorted(os.listdir(SAMPLE_DIR)):
                p = os.path.join(SAMPLE_DIR, f)
                if f.endswith(".caj") and detect_caj_type(p) == "pdf-embedded":
                    src = p
                    break
        if not src:
            pytest.skip("本机无 pdf-embedded 样本")

        import asyncio

        out = str(tmp_path / "converted.pdf")
        ok, msg, ftype = asyncio.run(convert_caj_to_pdf(src, out))
        assert ok, msg
        assert ftype == "pdf-embedded"

        import fitz

        doc = fitz.open(out)
        assert doc.page_count >= 1
        text = "".join(pg.get_text() for pg in doc)
        doc.close()
        # 原生 PDF：文字层可搜索
        assert len(text) > 100

    def test_kdh_rejected(self, tmp_path):
        p = _make(str(tmp_path / "k.caj"), b"KDH\x00\x02" + b"\x00" * 50)

        import asyncio

        ok, msg, ftype = asyncio.run(convert_caj_to_pdf(p, str(tmp_path / "o.pdf")))
        assert not ok
        assert ftype == "KDH"
        assert "暂不支持" in msg
