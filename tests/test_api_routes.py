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


def _pdf_with_image_bytes() -> bytes:
    """生成含一张内嵌 PNG 图片的 PDF 字节流（供 extract-images 用）。"""
    import fitz
    from PIL import Image

    img = Image.new("RGB", (60, 40), (200, 30, 30))
    img_buf = io.BytesIO()
    img.save(img_buf, "PNG")

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(fitz.Rect(50, 50, 200, 200), stream=img_buf.getvalue())
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


def _protected_pdf_bytes(user_pwd: str) -> bytes:
    """生成带打开密码的 PDF 字节流（供 unlock 用）。"""
    w = PdfWriter()
    w.append(io.BytesIO(PDF_2))
    w.encrypt(user_password=user_pwd, use_128bit=True)
    buf = io.BytesIO()
    w.write(buf)
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
        files={"file": ("myreport.pdf", PDF_3, "application/pdf")},
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
    # 下载文件名 = 原始上传文件主干 + .zip（而非 split-result.zip）
    assert 'filename="myreport.zip"' in dl.headers["content-disposition"]


def test_split_zip_download_name_matches_upload_chinese(client):
    """拆分包下载名与上传文件名一致（中文名，RFC 5987 filename* 编码）。"""
    r = client.post(
        "/api/v1/pdf/split",
        files={"file": ("季度报告.pdf", PDF_3, "application/pdf")},
        data={"mode": "all"},
    )
    assert r.status_code == 200
    dl = client.get(r.json()["download_url"])
    assert dl.status_code == 200
    assert dl.content[:2] == b"PK"
    disp = dl.headers["content-disposition"]
    # ASCII 通道应回退为 zip 通用名，UTF-8 通道携带原始主干
    assert "filename*=utf-8''" in disp
    from urllib.parse import unquote

    utf8_name = unquote(disp.split("utf-8''", 1)[1].split(";")[0].strip('" '))
    assert utf8_name == "季度报告.zip"


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


def test_extract_images_happy_path(client):
    r = client.post(
        "/api/v1/pdf/extract-images",
        files={"file": ("input.pdf", _pdf_with_image_bytes(), "application/pdf")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert body["file_count"] == 1

    dl = client.get(body["download_url"])
    assert dl.status_code == 200
    assert dl.content[:2] == b"PK"  # zip 文件头


def test_extract_images_no_image_returns_422(client):
    r = client.post(
        "/api/v1/pdf/extract-images",
        files={"file": ("input.pdf", PDF_2, "application/pdf")},
    )
    assert r.status_code == 422


def test_unlock_happy_path_with_password(client):
    r = client.post(
        "/api/v1/pdf/unlock",
        files={"file": ("locked.pdf", _protected_pdf_bytes("secret123"), "application/pdf")},
        data={"password": "secret123"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "completed"
    assert "解除" in body["message"]

    dl = client.get(body["download_url"])
    assert dl.status_code == 200
    assert dl.content[:4] == b"%PDF"


def test_unlock_wrong_password_returns_422(client):
    r = client.post(
        "/api/v1/pdf/unlock",
        files={"file": ("locked.pdf", _protected_pdf_bytes("secret123"), "application/pdf")},
        data={"password": "wrong"},
    )
    assert r.status_code == 422
    assert "密码" in r.json()["detail"]


def test_unlock_not_encrypted_passthrough(client):
    r = client.post(
        "/api/v1/pdf/unlock",
        files={"file": ("plain.pdf", PDF_2, "application/pdf")},
    )
    assert r.status_code == 200
    assert "未设置密码" in r.json()["message"]


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
