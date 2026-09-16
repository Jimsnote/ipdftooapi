"""旋转 PDF 与 PDF 加水印的 service + 路由测试。

覆盖：
- rotate：全部页/指定页/叠加语义/角度与页码校验
- watermark：文字 tile/center、图片 center、参数校验、透明度
- 路由层：analyze → rotate/watermark → download 全链路 + 错误分支
"""

import io

import fitz
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.services.pdf_rotator import PDFRotator, normalize_rotation
from app.services.pdf_watermarker import PDFWatermarker


def _pdf_bytes(num_pages: int, text: str = "body text") -> bytes:
    doc = fitz.open()
    for i in range(num_pages):
        page = doc.new_page(width=595.27, height=841.89)
        page.insert_text(fitz.Point(72, 100), f"page {i + 1} {text}", fontsize=12)
    data = doc.tobytes()
    doc.close()
    return data


def _png_bytes() -> bytes:
    img = Image.new("RGBA", (200, 80), (200, 30, 30, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------- normalize_rotation ----------


class TestNormalizeRotation:
    def test_positive(self):
        assert normalize_rotation(90) == 90

    def test_wrap_around(self):
        assert normalize_rotation(270 + 180) == 90

    def test_negative(self):
        """脏文件可能带负角度： naive % 360 会得负值。"""
        assert normalize_rotation(-90) == 270


# ---------- rotate service ----------


class TestRotateService:
    def test_rotate_all_pages(self, tmp_path):
        src = tmp_path / "in.pdf"
        src.write_bytes(_pdf_bytes(3))
        rotator = PDFRotator(str(src))
        total, rotated = rotator.rotate(90, str(tmp_path / "out.pdf"))
        assert (total, rotated) == (3, 3)
        doc = fitz.open(str(tmp_path / "out.pdf"))
        assert all(p.rotation == 90 for p in doc)
        doc.close()

    def test_rotation_stack_semantics(self, tmp_path):
        """已有 90° 的页面再顺时针转 270° 应归一化为 0。"""
        src = tmp_path / "in.pdf"
        src.write_bytes(_pdf_bytes(1))
        rotator = PDFRotator(str(src))
        rotator.rotate(90, str(tmp_path / "step1.pdf"))
        second = PDFRotator(str(tmp_path / "step1.pdf"))
        second.rotate(270, str(tmp_path / "out.pdf"))
        doc = fitz.open(str(tmp_path / "out.pdf"))
        assert doc[0].rotation == 0
        doc.close()

    def test_same_output_path_rejected(self, tmp_path):
        src = tmp_path / "in.pdf"
        src.write_bytes(_pdf_bytes(1))
        rotator = PDFRotator(str(src))
        with pytest.raises(ValueError, match="不能与输入"):
            rotator.rotate(90, str(src))

    def test_selected_pages_only(self, tmp_path):
        src = tmp_path / "in.pdf"
        src.write_bytes(_pdf_bytes(3))
        rotator = PDFRotator(str(src))
        _, rotated = rotator.rotate(
            90, str(tmp_path / "out.pdf"), pages={1, 3}
        )
        assert rotated == 2
        doc = fitz.open(str(tmp_path / "out.pdf"))
        assert doc[0].rotation == 90
        assert doc[1].rotation == 0
        assert doc[2].rotation == 90
        doc.close()

    def test_invalid_angle_rejected(self, tmp_path):
        src = tmp_path / "in.pdf"
        src.write_bytes(_pdf_bytes(1))
        rotator = PDFRotator(str(src))
        with pytest.raises(ValueError, match="旋转角度"):
            rotator.rotate(45, str(tmp_path / "out.pdf"))

    def test_empty_page_selection_rejected(self, tmp_path):
        src = tmp_path / "in.pdf"
        src.write_bytes(_pdf_bytes(1))
        rotator = PDFRotator(str(src))
        with pytest.raises(ValueError, match="页码"):
            rotator.rotate(90, str(tmp_path / "out.pdf"), pages=set())

    def test_parse_page_list_range(self):
        assert PDFRotator.parse_page_list("1,3-5,8", 10) == {1, 3, 4, 5, 8}

    def test_parse_page_list_reversed_range_rejected(self):
        with pytest.raises(ValueError, match="区间无效"):
            PDFRotator.parse_page_list("5-2", 10)

    def test_content_preserved(self, tmp_path):
        """旋转不重排内容流：正文文字必须原样保留。"""
        src = tmp_path / "in.pdf"
        src.write_bytes(_pdf_bytes(2, text="KEEPME body"))
        rotator = PDFRotator(str(src))
        rotator.rotate(180, str(tmp_path / "out.pdf"))
        doc = fitz.open(str(tmp_path / "out.pdf"))
        assert "KEEPME body" in doc[0].get_text()
        doc.close()


# ---------- watermark service ----------


class TestWatermarkService:
    def test_text_tile(self, tmp_path):
        src = tmp_path / "in.pdf"
        src.write_bytes(_pdf_bytes(2))
        wm = PDFWatermarker(str(src))
        total, applied = wm.watermark_text(
            "内部资料", str(tmp_path / "out.pdf"), opacity=0.2, layout="tile"
        )
        assert (total, applied) == (2, 2)
        doc = fitz.open(str(tmp_path / "out.pdf"))
        assert "page 1 body text" in doc[0].get_text()  # 正文保留
        assert "内部资料" in doc[0].get_text()  # 水印已加
        doc.close()

    def test_text_center_selected_pages(self, tmp_path):
        src = tmp_path / "in.pdf"
        src.write_bytes(_pdf_bytes(3))
        wm = PDFWatermarker(str(src))
        _, applied = wm.watermark_text(
            "CONFIDENTIAL",
            str(tmp_path / "out.pdf"),
            layout="center",
            color="red",
            pages={2},
        )
        assert applied == 1
        doc = fitz.open(str(tmp_path / "out.pdf"))
        assert "CONFIDENTIAL" in doc[1].get_text()
        doc.close()

    def test_text_validations(self, tmp_path):
        src = tmp_path / "in.pdf"
        src.write_bytes(_pdf_bytes(1))
        wm = PDFWatermarker(str(src))
        with pytest.raises(ValueError, match="不能为空"):
            wm.watermark_text("   ", str(tmp_path / "out.pdf"))
        with pytest.raises(ValueError, match="过长"):
            wm.watermark_text("长" * 61, str(tmp_path / "out.pdf"))
        with pytest.raises(ValueError, match="颜色"):
            wm.watermark_text("ok", str(tmp_path / "out.pdf"), color="pink")

    def test_image_center(self, tmp_path):
        src = tmp_path / "in.pdf"
        src.write_bytes(_pdf_bytes(1))
        wm = PDFWatermarker(str(src))
        _, applied = wm.watermark_image(
            _png_bytes(), str(tmp_path / "out.pdf"), opacity=0.3, layout="center"
        )
        assert applied == 1
        doc = fitz.open(str(tmp_path / "out.pdf"))
        assert len(doc[0].get_images()) == 1
        assert "page 1 body text" in doc[0].get_text()
        doc.close()

    def test_opacity_clamped(self, tmp_path):
        from app.services.pdf_watermarker import _clamp_opacity

        assert _clamp_opacity(0.01) == 0.05
        assert _clamp_opacity(0.9) == 0.5
        assert _clamp_opacity(0.2) == 0.2


# ---------- 路由层 ----------


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient，并把 pdf 路由的 TEMP_DIR 重定向到临时目录。"""
    import app.routers.pdf as pdf_router

    monkeypatch.setattr(pdf_router, "TEMP_DIR", str(tmp_path))

    from app.main import app

    return TestClient(app)


def _analyze(client, pdf: bytes, name="doc.pdf"):
    r = client.post(
        "/api/v1/pdf/analyze",
        files={"file": (name, pdf, "application/pdf")},
    )
    assert r.status_code == 200
    return r.json()["task_id"]


class TestRotateRoute:
    def test_happy_path_all_pages(self, client):
        task_id = _analyze(client, _pdf_bytes(2))
        r = client.post(
            "/api/v1/pdf/rotate",
            data={"task_id": task_id, "angle": "90", "pages": ""},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "completed"
        assert "全部页面" in body["message"]
        dl = client.get(body["download_url"])
        assert dl.status_code == 200
        doc = fitz.open(stream=dl.content, filetype="pdf")
        assert all(p.rotation == 90 for p in doc)
        doc.close()

    def test_selected_pages_message(self, client):
        task_id = _analyze(client, _pdf_bytes(3))
        r = client.post(
            "/api/v1/pdf/rotate",
            data={"task_id": task_id, "angle": "180", "pages": "1,3"},
        )
        assert r.status_code == 200
        assert "指定 2 页" in r.json()["message"]

    def test_invalid_angle_400(self, client):
        task_id = _analyze(client, _pdf_bytes(1))
        r = client.post(
            "/api/v1/pdf/rotate",
            data={"task_id": task_id, "angle": "45"},
        )
        assert r.status_code == 400
        assert "旋转角度" in r.json()["detail"]

    def test_invalid_pages_400(self, client):
        task_id = _analyze(client, _pdf_bytes(2))
        r = client.post(
            "/api/v1/pdf/rotate",
            data={"task_id": task_id, "angle": "90", "pages": "99-200"},
        )
        assert r.status_code == 400
        assert "页码" in r.json()["detail"]

    def test_missing_task_404(self, client):
        r = client.post(
            "/api/v1/pdf/rotate",
            data={"task_id": "00000000-0000-0000-0000-000000000000", "angle": "90"},
        )
        assert r.status_code == 404


class TestWatermarkRoute:
    def test_text_happy_path(self, client):
        task_id = _analyze(client, _pdf_bytes(2))
        r = client.post(
            "/api/v1/pdf/watermark",
            data={
                "task_id": task_id,
                "wm_type": "text",
                "text": "公司内部资料",
                "layout": "tile",
                "opacity": "0.2",
                "color": "gray",
                "fontsize": "48",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "completed"
        dl = client.get(body["download_url"])
        assert dl.status_code == 200
        doc = fitz.open(stream=dl.content, filetype="pdf")
        assert "公司内部资料" in doc[0].get_text()
        doc.close()

    def test_image_happy_path(self, client):
        task_id = _analyze(client, _pdf_bytes(1))
        r = client.post(
            "/api/v1/pdf/watermark",
            data={
                "task_id": task_id,
                "wm_type": "image",
                "layout": "center",
                "opacity": "0.3",
                "width_fraction": "0.4",
            },
            files={"wm_image": ("stamp.png", _png_bytes(), "image/png")},
        )
        assert r.status_code == 200
        dl = client.get(r.json()["download_url"])
        doc = fitz.open(stream=dl.content, filetype="pdf")
        assert len(doc[0].get_images()) == 1
        doc.close()

    def test_image_missing_400(self, client):
        task_id = _analyze(client, _pdf_bytes(1))
        r = client.post(
            "/api/v1/pdf/watermark",
            data={"task_id": task_id, "wm_type": "image"},
        )
        assert r.status_code == 400
        assert "水印图片" in r.json()["detail"]

    def test_image_magic_mismatch_400(self, client):
        task_id = _analyze(client, _pdf_bytes(1))
        r = client.post(
            "/api/v1/pdf/watermark",
            data={"task_id": task_id, "wm_type": "image"},
            files={"wm_image": ("fake.png", b"not-a-png", "image/png")},
        )
        assert r.status_code == 400
        assert "不符" in r.json()["detail"]

    def test_empty_text_400(self, client):
        task_id = _analyze(client, _pdf_bytes(1))
        r = client.post(
            "/api/v1/pdf/watermark",
            data={"task_id": task_id, "wm_type": "text", "text": "  "},
        )
        assert r.status_code == 400

    def test_missing_task_404(self, client):
        r = client.post(
            "/api/v1/pdf/watermark",
            data={"task_id": "00000000-0000-0000-0000-000000000000", "wm_type": "text", "text": "x"},
        )
        assert r.status_code == 404
