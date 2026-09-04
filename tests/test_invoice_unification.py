# -*- coding: utf-8 -*-
"""发票合并统一化阶段①回归：混合 PDF/OFD 分流（docs/INVOICE_MERGE_UNIFICATION_PLAN.md §5.1）。

覆盖统一端点 /api/v1/invoice/analyze 的三态上传：
- 纯 PDF（回归：与旧版行为一致）
- 混合 PDF + OFD（新增：.ofd 后缀走锁内转换+归一化，invoice_NNN.pdf 序号连续）
- 坏 OFD + 好 PDF → fail-fast 400（发票合并不允许部分成功）

OFD 样本缺失时相关用例自动跳过（样本目录同 test_ofd_rendering.py）。
TEMP_DIR 重定向到 pytest 临时目录，不污染项目 ./temp。
"""
import io
import os
import uuid

import pytest
from pypdf import PdfWriter
from fastapi.testclient import TestClient

fitz = pytest.importorskip("fitz")

from app.main import app  # noqa: E402

_SAMPLE_DIRS = [
    r"C:/home/小红书",
    os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "docs", "xml-invoice", "ofd")
    ),
]


def _find_ofd_sample():
    for d in _SAMPLE_DIRS:
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if name.lower().endswith(".ofd"):
                return name, os.path.join(d, name)
    return None, None


def _pdf_bytes(num_pages: int = 1) -> bytes:
    buf = io.BytesIO()
    writer = PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=595.27, height=841.89)
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    import app.routers.invoice as invoice_router
    import app.routers.ofd_invoice as ofd_router

    monkeypatch.setattr(invoice_router, "TEMP_DIR", str(tmp_path))
    monkeypatch.setattr(ofd_router, "TEMP_DIR", str(tmp_path))
    return TestClient(app)


OFD_NAME, OFD_PATH = _find_ofd_sample()
has_ofd = OFD_PATH is not None
pytestmark_ofd = pytest.mark.skipif(not has_ofd, reason="无 OFD 样本可用")


def test_mixed_analyze_pdf_only_regression(client):
    """纯 PDF：与旧版行为一致（回归保护）。"""
    r = client.post(
        "/api/v1/invoice/analyze",
        files=[
            ("files", ("a.pdf", _pdf_bytes(2), "application/pdf")),
            ("files", ("b.pdf", _pdf_bytes(1), "application/pdf")),
        ],
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total_count"] == 2
    assert body["invoices"][0]["filename"] == "a.pdf"
    assert body["invoices"][0]["page_count"] == 2


@pytestmark_ofd
def test_mixed_analyze_pdf_plus_ofd(client):
    """混合批次：PDF×2 + OFD×1 → 3 条结果，序号连续对齐上传顺序。"""
    with open(OFD_PATH, "rb") as f:
        ofd_bytes = f.read()

    # 故意 OFD 放中间，验证序号对齐（invoice_002 是 OFD 转换产物）
    r = client.post(
        "/api/v1/invoice/analyze",
        files=[
            ("files", ("first.pdf", _pdf_bytes(1), "application/pdf")),
            ("files", (OFD_NAME, ofd_bytes, "application/octet-stream")),
            ("files", ("third.pdf", _pdf_bytes(1), "application/pdf")),
        ],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_count"] == 3
    # 返回顺序 = 上传顺序（含原始文件名）
    assert [inv["filename"] for inv in body["invoices"]] == ["first.pdf", OFD_NAME, "third.pdf"]
    # OFD 转换产物尺寸合理（归一化后 ~A4 宽度级别，非 MediaBox 放大态）
    ofd_w = body["invoices"][1]["width"]
    assert 200 < ofd_w < 1200, f"OFD 归一化后宽度异常: {ofd_w}"

    # task 目录里 3 个 invoice_NNN.pdf 序号连续
    task_id = body["task_id"]
    import app.routers.invoice as invoice_router

    task_dir = os.path.join(str(invoice_router.TEMP_DIR), task_id)
    pdfs = sorted(f for f in os.listdir(task_dir) if f.startswith("invoice_") and f.endswith(".pdf"))
    assert pdfs == ["invoice_001.pdf", "invoice_002.pdf", "invoice_003.pdf"], pdfs
    # OFD 原件另存 input_NNN.ofd 便于回溯
    inputs = [f for f in os.listdir(task_dir) if f.startswith("input_") and f.endswith(".ofd")]
    assert inputs == ["input_002.ofd"], inputs


@pytestmark_ofd
def test_mixed_merge_end_to_end(client):
    """混合批次 merge：analyze → merge → 输出 PDF 页数正确。"""
    with open(OFD_PATH, "rb") as f:
        ofd_bytes = f.read()

    r = client.post(
        "/api/v1/invoice/analyze",
        files=[
            ("files", ("a.pdf", _pdf_bytes(1), "application/pdf")),
            ("files", (OFD_NAME, ofd_bytes, "application/octet-stream")),
        ],
    )
    assert r.status_code == 200, r.text
    task_id = r.json()["task_id"]

    r2 = client.post(
        "/api/v1/invoice/merge",
        data={"task_id": task_id, "per_page": 4, "margin": "standard",
              "crop_marks": "true", "page_numbers": "true"},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["status"] == "completed"
    assert body["file_count"] == 1

    dl = client.get(f"/api/v1/invoice/download/{task_id}?preview=true")
    assert dl.status_code == 200
    assert dl.content[:4] == b"%PDF"


@pytestmark_ofd
def test_mixed_analyze_bad_ofd_fail_fast(client):
    """坏 OFD + 好 PDF → 整批 400，错误信息指明第 N 个文件，中间产物已清理。

    注意：魔数拦截（非 ZIP 头）会在上传层直接 415，不进入转换流程；
    本用例构造「ZIP 头合法但内容非 OFD」的文件，覆盖转换层 fail-fast。
    """
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("dummy.txt", "not an ofd")
    bad_ofd_bytes = buf.getvalue()

    r = client.post(
        "/api/v1/invoice/analyze",
        files=[
            ("files", ("good.pdf", _pdf_bytes(1), "application/pdf")),
            ("files", ("fake.ofd", bad_ofd_bytes, "application/octet-stream")),
        ],
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "第 2 个文件" in detail
    assert "fake.ofd" in detail

    # task 目录里不应残留半成品 invoice PDF（fail-fast 清理）
    # 400 响应体里没有 task_id，直接扫 TEMP_DIR 下无新增任务目录或其内无 invoice_*.pdf
    import app.routers.invoice as invoice_router

    leftover = []
    for d in os.listdir(str(invoice_router.TEMP_DIR)):
        td = os.path.join(str(invoice_router.TEMP_DIR), d)
        if os.path.isdir(td):
            for f in os.listdir(td):
                if f.startswith("invoice_") and f.endswith(".pdf"):
                    leftover.append(os.path.join(d, f))
    assert not leftover, f"fail-fast 未清理中间产物: {leftover}"


def test_ofd_endpoint_regression(client):
    """旧端点 /api/v1/ofd-invoice/analyze 行为不变（重构等价性）。"""
    if not has_ofd:
        pytest.skip("无 OFD 样本可用")
    with open(OFD_PATH, "rb") as f:
        ofd_bytes = f.read()
    r = client.post(
        "/api/v1/ofd-invoice/analyze",
        files=[("files", (OFD_NAME, ofd_bytes, "application/octet-stream"))],
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_count"] == 1
    assert body["invoices"][0]["filename"] == OFD_NAME
