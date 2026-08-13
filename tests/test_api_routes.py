"""路由层接口集成测试（TestClient）。

覆盖 health、pdf、invoice 三个路由的 HTTP 行为：正常处理链路、
参数/类型校验、错误处理、下载接口的 404 分支。

这些测试通过 monkeypatch 把各路由的 TEMP_DIR 指向 pytest 的临时目录，
避免污染项目 ./temp，且每次运行互相隔离。

依赖：app.main 可导入（pydantic 2.7+、pillow/numpy 原生扩展可用、
httpx<0.28）。在 Python 3.11/3.12 的干净环境下一键可跑。
"""

import io
import uuid

import pytest
from pypdf import PdfWriter
from fastapi.testclient import TestClient


def _pdf_bytes(num_pages: int) -> bytes:
    """生成合法的空白多页 PDF 字节流（A4 尺寸）。"""
    buf = io.BytesIO()
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=595.27, height=841.89)
    writer.write(buf)
    return buf.getvalue()


PDF_3 = _pdf_bytes(3)
PDF_2 = _pdf_bytes(2)


@pytest.fixture
def client(tmp_path, monkeypatch):
    """TestClient，并把 pdf/invoice/id_photo 路由的 TEMP_DIR 重定向到临时目录。"""
    import app.routers.pdf as pdf_router
    import app.routers.invoice as invoice_router
    import app.routers.id_photo as idphoto_router

    monkeypatch.setattr(pdf_router, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(invoice_router, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(idphoto_router, "TEMP_DIR", str(tmp_path))

    from app.main import app

    return TestClient(app)


# ---------- Health ----------

def test_health_ok(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "ipdftoo-api"


# ---------- PDF 路由正常链路 ----------

def test_split_happy_path(client):
    r = client.post(
        "/api/v1/pdf/split",
        files={"file": ("input.pdf", PDF_3, "application/pdf")},
        data={"mode": "all"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["file_count"] == 3
    assert body["download_url"].startswith("/api/v1/pdf/download/")

    # 下载返回的应是 zip（多页分割）
    dl = client.get(body["download_url"])
    assert dl.status_code == 200
    assert dl.content[:2] == b"PK"  # zip 文件头


def test_merge_happy_path(client):
    r = client.post(
        "/api/v1/pdf/merge",
        files=[
            ("files", ("a.pdf", PDF_2, "application/pdf")),
            ("files", ("b.pdf", PDF_3, "application/pdf")),
        ],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["file_count"] == 1

    dl = client.get(body["download_url"])
    assert dl.status_code == 200
    assert dl.content[:4] == b"%PDF"


def test_analyze_returns_page_count(client):
    r = client.post(
        "/api/v1/pdf/analyze",
        files={"file": ("input.pdf", PDF_3, "application/pdf")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total_pages"] == 3
    assert body["filename"] == "input.pdf"


def test_protect_happy_path(client):
    r = client.post(
        "/api/v1/pdf/protect",
        files={"file": ("input.pdf", PDF_2, "application/pdf")},
        data={"password": "secret123"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["download_url"].startswith("/api/v1/pdf/download/")


# ---------- 参数 / 类型校验 ----------

def test_protect_short_password_rejected(client):
    r = client.post(
        "/api/v1/pdf/protect",
        files={"file": ("input.pdf", PDF_2, "application/pdf")},
        data={"password": "123"},
    )
    assert r.status_code == 400
    assert "6" in r.json()["detail"]


def test_split_wrong_extension_returns_415(client):
    # 扩展名不在 PDF_EXTENSIONS 内 → 415
    r = client.post(
        "/api/v1/pdf/split",
        files={"file": ("input.txt", b"%PDF-1.4 fake", "text/plain")},
        data={"mode": "all"},
    )
    assert r.status_code == 415


def test_split_bad_magic_returns_415(client):
    # 扩展名是 .pdf，但文件头不是 %PDF- → 415
    r = client.post(
        "/api/v1/pdf/split",
        files={"file": ("input.pdf", b"this is definitely not a pdf", "application/pdf")},
        data={"mode": "all"},
    )
    assert r.status_code == 415


def test_merge_exceeds_max_files(client):
    files = [
        ("files", (f"f{i}.pdf", PDF_2, "application/pdf"))
        for i in range(21)  # MAX_FILES_PER_REQUEST = 20
    ]
    r = client.post("/api/v1/pdf/merge", files=files)
    assert r.status_code == 400


# ---------- 下载接口错误分支 ----------

def test_download_missing_task_returns_404(client):
    fake_id = str(uuid.uuid4())
    r = client.get(f"/api/v1/pdf/download/{fake_id}")
    assert r.status_code == 404


def test_download_invalid_task_id_returns_400(client):
    r = client.get("/api/v1/pdf/download/not-a-uuid")
    assert r.status_code == 400


# ---------- Invoice 路由 ----------

def test_invoice_analyze_returns_dimensions(client):
    r = client.post(
        "/api/v1/invoice/analyze",
        files=[("files", ("inv.pdf", PDF_2, "application/pdf"))],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total_count"] == 1
    assert body["invoices"][0]["page_count"] == 2
    assert body["invoices"][0]["width"] > 0
