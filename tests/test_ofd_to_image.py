"""OFD 转图片路由测试。

- 参数校验（DPI 白名单）与坏文件拦截：不依赖样本，永远执行；
- 真实样本端到端：复用 test_ofd_rendering 的样本目录收集策略
  （本地有样本则参数化全跑，无样本自动 skip——与现有约定一致）。
"""
import io
import os
import sys
import zipfile

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import app  # noqa: E402
from app.routers import ofd as ofd_router  # noqa: E402
from app.routers import pdf as pdf_router  # noqa: E402

client = TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _isolate_temp(tmp_path, monkeypatch):
    d = tmp_path / "temp"
    d.mkdir()
    monkeypatch.setattr(ofd_router, "TEMP_DIR", str(d))
    monkeypatch.setattr(pdf_router, "TEMP_DIR", str(d))


class TestDpiValidation:
    def test_invalid_dpi_rejected_before_upload(self):
        r = client.post(
            "/api/v1/ofd/to-image",
            files={"file": ("a.ofd", b"PK\x03\x04", "application/ofd")},
            data={"dpi": "200"},
        )
        assert r.status_code == 400
        assert "96 / 150 / 300" in r.json()["detail"]


class TestBadFile:
    def test_garbage_zip_rejected(self):
        r = client.post(
            "/api/v1/ofd/to-image",
            files={"file": ("a.ofd", b"PK\x03\x04" + b"\x00" * 512, "application/ofd")},
            data={"format": "jpg", "dpi": "150"},
        )
        assert r.status_code == 422

    def test_wrong_magic_rejected(self):
        r = client.post(
            "/api/v1/ofd/to-image",
            files={"file": ("a.ofd", b"not a zip at all", "application/ofd")},
            data={"format": "jpg", "dpi": "150"},
        )
        assert r.status_code in (400, 415, 422)


class TestParamHardening:
    """对抗审查 R1：format 白名单（'svg'/'JPG'/'jpg;../x' 曾静默当 PNG 放行）。"""

    @pytest.mark.parametrize("fmt", ["svg", "jpg;../x", "..", "png\x00", "jpeg"])
    def test_format_not_whitelist_rejected(self, fmt):
        r = client.post(
            "/api/v1/ofd/to-image",
            files={"file": ("a.ofd", b"PK\x03\x04", "application/ofd")},
            data={"format": fmt, "dpi": "96"},
        )
        assert r.status_code == 400
        assert "jpg / png" in r.json()["detail"]

    def test_format_case_normalized(self, monkeypatch):
        """大写/大小写混合归一化放行（fake 转换器隔离，断言 format 传递）。"""

        async def _fake_convert(input_path, output_path, crop=True):
            return True, "ok"

        monkeypatch.setattr(ofd_router, "convert_ofd_to_pdf", _fake_convert)
        monkeypatch.setattr(ofd_router, "count_pdf_pages", lambda _p: 1)

        captured = {}

        class _Fake:
            def __init__(self, p):
                pass

            def convert(self, output_dir, format="jpg", dpi=150, pages="all"):
                captured["format"] = format
                d = output_dir + "/x.jpg"
                open(d, "wb").write(b"\xff\xd8\xff\xe0")
                return [d]

            zip_images = staticmethod(lambda ps, zp: zp)

        monkeypatch.setattr(ofd_router, "PDFToImageConverter", _Fake)

        sample = SAMPLES[0][1]
        with open(sample, "rb") as f:
            r = client.post(
                "/api/v1/ofd/to-image",
                files={"file": ("a.ofd", f, "application/ofd")},
                data={"format": "JPG", "dpi": "96"},
            )
        assert r.status_code == 200
        assert captured["format"] == "jpg"


class TestRenderGuards:
    """对抗审查 R2：像素预算超限、0 页、页数上限、大文档降档（fake 转换器隔离渲染）。"""

    def _patch_converter(self, monkeypatch, tmp_path, mode):
        """mode: valueerror | empty | ok —— ok 返回真实临时 jpg。"""
        from app.routers import ofd as ofd_router

        dummy = tmp_path / "x.jpg"
        dummy.write_bytes(b"\xff\xd8\xff\xe0")
        captured = {}

        class _FakeConverter:
            def __init__(self, path: str):
                pass

            def convert(self, output_dir, format="jpg", dpi=150, pages="all"):
                captured["dpi"] = dpi
                if mode == "valueerror":
                    raise ValueError("第 1 页尺寸过大（约 12000×12000 像素），超出处理上限")
                if mode == "empty":
                    return []
                return [str(dummy)]

            zip_images = staticmethod(lambda paths, zp: zp)

        monkeypatch.setattr(ofd_router, "PDFToImageConverter", _FakeConverter)
        return captured

    def _post_real(self, monkeypatch, pages=1):
        from app.routers import ofd as ofd_router

        async def _fake_convert(input_path, output_path, crop=True):
            return True, "ok"

        monkeypatch.setattr(ofd_router, "count_pdf_pages", lambda _p: pages)
        monkeypatch.setattr(ofd_router, "convert_ofd_to_pdf", _fake_convert)
        sample = SAMPLES[0][1]
        with open(sample, "rb") as f:
            return client.post(
                "/api/v1/ofd/to-image",
                files={"file": ("a.ofd", f, "application/ofd")},
                data={"format": "jpg", "dpi": "300"},
            )

    def test_oversize_page_value_error_hint(self, monkeypatch, tmp_path):
        self._patch_converter(monkeypatch, tmp_path, "valueerror")
        r = self._post_real(monkeypatch, pages=1)
        assert r.status_code == 422
        assert "更低的 DPI" in r.json()["detail"]

    def test_zero_page_rejected(self, monkeypatch, tmp_path):
        self._patch_converter(monkeypatch, tmp_path, "empty")
        r = self._post_real(monkeypatch, pages=0)
        assert r.status_code == 422
        assert "未能" in r.json()["detail"]

    def test_over_200_pages_rejected(self, monkeypatch, tmp_path):
        self._patch_converter(monkeypatch, tmp_path, "ok")
        r = self._post_real(monkeypatch, pages=201)
        assert r.status_code == 422
        assert "200" in r.json()["detail"]

    def test_large_doc_30plus_downgrades_to_150(self, monkeypatch, tmp_path):
        captured = self._patch_converter(monkeypatch, tmp_path, "ok")
        r = self._post_real(monkeypatch, pages=60)
        assert r.status_code == 200
        assert captured["dpi"] == 150
        assert "自动降为 150" in r.json()["message"]

    def test_large_doc_80plus_downgrades_to_96(self, monkeypatch, tmp_path):
        captured = self._patch_converter(monkeypatch, tmp_path, "ok")
        r = self._post_real(monkeypatch, pages=100)
        assert r.status_code == 200
        assert captured["dpi"] == 96
        assert "自动降为 96" in r.json()["message"]


def _collect_samples():
    dirs = [
        r"C:/home/小红书",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs/xml-invoice/ofd"),
    ]
    out = []
    for d in dirs:
        if os.path.isdir(d):
            for n in sorted(os.listdir(d)):
                if n.lower().endswith(".ofd"):
                    out.append((n, os.path.join(d, n)))
    return out


SAMPLES = _collect_samples()

if not SAMPLES:
    collect_ignore = ["test_ofd_to_image.py"]


@pytest.mark.parametrize("name,path", SAMPLES, ids=[n for n, _ in SAMPLES])
def test_to_image_real_sample(name, path):
    """真实样本：转换+渲染+下载全链路；产物为单图或 ZIP。"""
    with open(path, "rb") as f:
        r = client.post(
            "/api/v1/ofd/to-image",
            files={"file": (name, f, "application/ofd")},
            data={"format": "jpg", "dpi": "96"},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["file_count"] >= 1

    d = client.get(body["download_url"])
    assert d.status_code == 200
    content = d.content
    if body["file_count"] == 1:
        # JPEG 魔数 FFD8 / PNG 魔数 8950
        assert content[:2] == b"\xff\xd8" or content[:2] == b"\x89P"
    else:
        zf = zipfile.ZipFile(io.BytesIO(content))
        names = zf.namelist()
        assert len(names) == body["file_count"]
        assert all(n.lower().endswith(".jpg") for n in names)
