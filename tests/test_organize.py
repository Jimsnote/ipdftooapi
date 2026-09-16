"""整理 PDF（页面重排/删除/复制/单页旋转）的 service + 路由测试。

覆盖：
- parse_order：重排/删除/复制/旋转/越界/空串/格式错误
- organize service：页序重建、单页旋转叠加、删除语义、内容跟随页移动、
  输出与输入同路径拒绝、metadata 清空
- 路由层：analyze → organize → download 全链路 + 错误分支
"""

import fitz
import pytest
from fastapi.testclient import TestClient

from app.services.pdf_organizer import PDFOrganizer, normalize_rotation, parse_order


def _pdf_bytes(num_pages: int, text: str = "body text") -> bytes:
    doc = fitz.open()
    for i in range(num_pages):
        page = doc.new_page(width=595.27, height=841.89)
        page.insert_text(fitz.Point(72, 100), f"page {i + 1} {text}", fontsize=12)
    data = doc.tobytes()
    doc.close()
    return data


def _pdf_with_metadata_and_rotation() -> bytes:
    """带 metadata 与页面旋转角的样本（验证重建语义）。"""
    doc = fitz.open()
    page = doc.new_page(width=595.27, height=841.89)
    page.insert_text(fitz.Point(72, 100), "page 1", fontsize=12)
    page.set_rotation(90)
    doc.new_page(width=595.27, height=841.89)
    doc.set_metadata({"title": "Secret Title", "author": "张三", "producer": "Word"})
    data = doc.tobytes()
    doc.close()
    return data


# ---------- normalize_rotation / parse_order ----------


class TestNormalizeRotation:
    def test_negative(self):
        assert normalize_rotation(-90) == 270

    def test_wrap(self):
        assert normalize_rotation(270 + 180) == 90


class TestParseOrder:
    def test_reorder(self):
        assert parse_order("3,1,2", 3) == [(2, 0), (0, 0), (1, 0)]

    def test_delete_by_omission(self):
        assert parse_order("1,3", 3) == [(0, 0), (2, 0)]

    def test_rotate_token(self):
        assert parse_order("2:90,1", 2) == [(1, 90), (0, 0)]

    def test_duplicate_allowed(self):
        assert parse_order("1,1", 1) == [(0, 0), (0, 0)]

    def test_empty_raises(self):
        with pytest.raises(ValueError, match="不能为空"):
            parse_order("  ,  ", 3)

    def test_out_of_range(self):
        with pytest.raises(ValueError, match="超出范围"):
            parse_order("1,4", 3)

    def test_bad_angle(self):
        with pytest.raises(ValueError, match="旋转角度"):
            parse_order("1:45", 1)

    def test_garbage(self):
        with pytest.raises(ValueError, match="格式无效"):
            parse_order("abc", 1)

    def test_double_colon(self):
        with pytest.raises(ValueError, match="格式无效"):
            parse_order("1:90:90", 1)

    def test_output_page_cap(self):
        """order 可复制页码：超上限（1000）必须拒绝，防资源放大。"""
        from app.services.pdf_organizer import MAX_OUTPUT_PAGES

        big = ",".join(["1"] * (MAX_OUTPUT_PAGES + 1))
        with pytest.raises(ValueError, match="输出页数过多"):
            parse_order(big, 1)


# ---------- organize service ----------


class TestOrganizeService:
    def test_reorder_content_follows(self, tmp_path):
        src = tmp_path / "in.pdf"
        src.write_bytes(_pdf_bytes(3))
        org = PDFOrganizer(str(src))
        total, out_total = org.organize(
            parse_order("3,1,2", 3), str(tmp_path / "out.pdf")
        )
        assert (total, out_total) == (3, 3)
        doc = fitz.open(str(tmp_path / "out.pdf"))
        texts = [p.get_text() for p in doc]
        assert "page 3" in texts[0]
        assert "page 1" in texts[1]
        assert "page 2" in texts[2]
        doc.close()

    def test_delete_pages(self, tmp_path):
        src = tmp_path / "in.pdf"
        src.write_bytes(_pdf_bytes(4))
        org = PDFOrganizer(str(src))
        _, out_total = org.organize(parse_order("1,3", 4), str(tmp_path / "out.pdf"))
        assert out_total == 2
        doc = fitz.open(str(tmp_path / "out.pdf"))
        texts = "".join(p.get_text() for p in doc)
        assert "page 2" not in texts and "page 4" not in texts
        assert "page 1" in texts and "page 3" in texts
        doc.close()

    def test_duplicate_page(self, tmp_path):
        src = tmp_path / "in.pdf"
        src.write_bytes(_pdf_bytes(2))
        org = PDFOrganizer(str(src))
        _, out_total = org.organize(parse_order("1,1", 2), str(tmp_path / "out.pdf"))
        assert out_total == 2

    def test_single_page_rotation(self, tmp_path):
        src = tmp_path / "in.pdf"
        src.write_bytes(_pdf_bytes(2))
        org = PDFOrganizer(str(src))
        org.organize(parse_order("2:90,1", 2), str(tmp_path / "out.pdf"))
        doc = fitz.open(str(tmp_path / "out.pdf"))
        assert doc[0].rotation == 90
        assert doc[1].rotation == 0
        doc.close()

    def test_rotation_stacks_on_existing(self, tmp_path):
        """原页 /Rotate=90 再叠加 90 → 180。"""
        src = tmp_path / "in.pdf"
        doc = fitz.open()
        page = doc.new_page(width=595.27, height=841.89)
        page.set_rotation(90)
        src.write_bytes(doc.tobytes())
        doc.close()

        org = PDFOrganizer(str(src))
        org.organize(parse_order("1:90", 1), str(tmp_path / "out.pdf"))
        out = fitz.open(str(tmp_path / "out.pdf"))
        assert out[0].rotation == 180
        out.close()

    def test_metadata_scrubbed(self, tmp_path):
        """重建产物不应携带原文档 metadata（隐私副产品）。"""
        src = tmp_path / "in.pdf"
        src.write_bytes(_pdf_with_metadata_and_rotation())
        org = PDFOrganizer(str(src))
        org.organize(parse_order("1,2", 2), str(tmp_path / "out.pdf"))
        doc = fitz.open(str(tmp_path / "out.pdf"))
        meta = doc.metadata or {}
        assert not (meta.get("title") or "").strip()
        assert not (meta.get("author") or "").strip()
        doc.close()

    def test_same_path_rejected(self, tmp_path):
        src = tmp_path / "in.pdf"
        src.write_bytes(_pdf_bytes(1))
        org = PDFOrganizer(str(src))
        with pytest.raises(ValueError, match="相同"):
            org.organize(parse_order("1", 1), str(src))

    def test_empty_order_rejected(self, tmp_path):
        src = tmp_path / "in.pdf"
        src.write_bytes(_pdf_bytes(1))
        org = PDFOrganizer(str(src))
        with pytest.raises(ValueError, match="不能为空"):
            org.organize([], str(tmp_path / "out.pdf"))

    def test_missing_input(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            PDFOrganizer(str(tmp_path / "nope.pdf"))


# ---------- 对抗审查 R2：数据一致性 ----------


class TestOrganizeRebuildSemantics:
    """重建路线的语义边界（service docstring 与 FAQ 已向用户声明）。"""

    def _pdf_with_extras(self) -> bytes:
        doc = fitz.open()
        p1 = doc.new_page(width=595.27, height=841.89)
        p1.insert_text(fitz.Point(72, 100), "page 1", fontsize=12)
        p2 = doc.new_page(width=595.27, height=841.89)
        p2.insert_text(fitz.Point(72, 100), "page 2", fontsize=12)
        p2.insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(72, 72, 200, 90), "page": 0})
        doc.set_toc([[1, "First", 1]])
        data = doc.tobytes()
        doc.close()
        return data

    def test_identity_order_preserves_content(self, tmp_path):
        """原序重建（1,2）后文本内容与页数不变。"""
        src = tmp_path / "in.pdf"
        src.write_bytes(self._pdf_with_extras())
        org = PDFOrganizer(str(src))
        _, out_total = org.organize(parse_order("1,2", 2), str(tmp_path / "out.pdf"))
        assert out_total == 2
        doc = fitz.open(str(tmp_path / "out.pdf"))
        text = "".join(p.get_text() for p in doc)
        assert "page 1" in text and "page 2" in text
        doc.close()

    def test_outline_dropped(self, tmp_path):
        """按文档声明：重建不保留书签（get_toc 为空）。"""
        src = tmp_path / "in.pdf"
        src.write_bytes(self._pdf_with_extras())
        org = PDFOrganizer(str(src))
        org.organize(parse_order("1,2", 2), str(tmp_path / "out.pdf"))
        doc = fitz.open(str(tmp_path / "out.pdf"))
        assert doc.get_toc() == []
        doc.close()

    def test_widget_value_preserved(self, tmp_path):
        """重建虽丢书签，但页面上的表单字段值应随页保留（未在丢弃声明内）。"""
        src = tmp_path / "in.pdf"
        doc = fitz.open()
        page = doc.new_page(width=595.27, height=841.89)
        w = fitz.Widget()
        w.field_name = "name"
        w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
        w.rect = fitz.Rect(72, 72, 250, 100)
        w.text_fontsize = 12
        w.text_font = "china-s"
        w.field_value = "张三"
        widget = page.add_widget(w)
        widget.update()
        src.write_bytes(doc.tobytes())
        doc.close()

        org = PDFOrganizer(str(src))
        org.organize(parse_order("1", 1), str(tmp_path / "out.pdf"))
        out = fitz.open(str(tmp_path / "out.pdf"))
        widgets = list(out[0].widgets() or [])
        assert len(widgets) == 1
        assert widgets[0].field_value == "张三"
        out.close()


# ---------- 对抗审查 R3：加密输入与重复提交 ----------


def _encrypted_pdf_bytes() -> bytes:
    doc = fitz.open()
    doc.new_page(width=595.27, height=841.89)
    data = doc.tobytes(encryption=2, owner_pw="o", user_pw="u")
    doc.close()
    return data


class TestEncryptedInputRejected:
    """加密文件必须前置拦截（400 引导解密），不得 500 或产出空文件。"""

    def test_organizer_rejects(self, tmp_path):
        src = tmp_path / "enc.pdf"
        src.write_bytes(_encrypted_pdf_bytes())
        with pytest.raises(ValueError, match="加密"):
            PDFOrganizer(str(src))

    def test_route_rejects_encrypted(self, client):
        """analyze 阶段即拦截加密文件（400 引导解密），不得 500。"""
        r = client.post(
            "/api/v1/pdf/analyze",
            files={"file": ("enc.pdf", _encrypted_pdf_bytes(), "application/pdf")},
        )
        assert r.status_code == 400
        assert "加密" in r.json()["detail"]


class TestOrganizeRepeatSubmit:
    """处理完成后 input.pdf 已删除：同一 task 重复提交必须 404，不得复用旧输出。"""

    def test_second_submit_404(self, client):
        task_id = _analyze(client, _pdf_bytes(2))
        r1 = client.post(
            "/api/v1/pdf/organize",
            data={"task_id": task_id, "order": "2,1"},
        )
        assert r1.status_code == 200
        r2 = client.post(
            "/api/v1/pdf/organize",
            data={"task_id": task_id, "order": "1,2"},
        )
        assert r2.status_code == 404


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


class TestOrganizeRoute:
    def test_happy_path_reorder(self, client):
        task_id = _analyze(client, _pdf_bytes(3))
        r = client.post(
            "/api/v1/pdf/organize",
            data={"task_id": task_id, "order": "3,1,2"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "completed"
        assert "3 页 -> 3 页" in body["message"]
        dl = client.get(body["download_url"])
        assert dl.status_code == 200
        doc = fitz.open(stream=dl.content, filetype="pdf")
        assert "page 3" in doc[0].get_text()
        doc.close()

    def test_delete_via_route(self, client):
        task_id = _analyze(client, _pdf_bytes(3))
        r = client.post(
            "/api/v1/pdf/organize",
            data={"task_id": task_id, "order": "1,3"},
        )
        assert r.status_code == 200
        assert "3 页 -> 2 页" in r.json()["message"]

    def test_rotate_via_route(self, client):
        task_id = _analyze(client, _pdf_bytes(2))
        r = client.post(
            "/api/v1/pdf/organize",
            data={"task_id": task_id, "order": "2:90,1"},
        )
        assert r.status_code == 200
        dl = client.get(r.json()["download_url"])
        doc = fitz.open(stream=dl.content, filetype="pdf")
        assert doc[0].rotation == 90
        doc.close()

    def test_empty_order_400(self, client):
        task_id = _analyze(client, _pdf_bytes(1))
        r = client.post(
            "/api/v1/pdf/organize",
            data={"task_id": task_id, "order": ""},
        )
        assert r.status_code == 400
        assert "不能为空" in r.json()["detail"]

    def test_out_of_range_400(self, client):
        task_id = _analyze(client, _pdf_bytes(2))
        r = client.post(
            "/api/v1/pdf/organize",
            data={"task_id": task_id, "order": "5"},
        )
        assert r.status_code == 400

    def test_missing_task_404(self, client):
        r = client.post(
            "/api/v1/pdf/organize",
            data={
                "task_id": "00000000-0000-0000-0000-000000000000",
                "order": "1",
            },
        )
        assert r.status_code == 404
