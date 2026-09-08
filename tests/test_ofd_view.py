# -*- coding: utf-8 -*-
"""OFD 在线查看（/api/v1/ofd/view）与 zip 预扫描测试。

覆盖 docs/OFD_VIEWER_DESIGN.md v1.1 §4.3 安全设计：
- 预扫描四规则（条目数 / 解压总量 / 压缩比 / zip slip 双保险）+ 加密检测
- 错误三分类 HTTP 文案（预扫描拦截 / 加密 / 转换失败）
- 正常转换链路（真实发票样本，无样本时自动跳过）
- 现有 /ofd-to-pdf 端点加固后的回归

加密 zip 说明：Python 标准库无法写入加密 zip，测试通过直接翻转
本地文件头（offset 6）与中央目录（offset 8）的 flag 低位字节构造，
与真实加密 OFD 的中央目录特征一致（PoC 已用 15 个真实样本验证判定依据）。
"""

import io
import os
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.services import ofd_validator
from app.services.ofd_validator import (
    OfdEncryptedError,
    OfdFileError,
    validate_ofd_zip,
)

_HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_DIRS = [
    r"C:/home/小红书",
    os.path.normpath(os.path.join(_HERE, "..", "..", "..", "docs", "xml-invoice", "ofd")),
]


def _collect_samples():
    files = []
    for d in SAMPLE_DIRS:
        if os.path.isdir(d):
            for name in os.listdir(d):
                if name.lower().endswith(".ofd"):
                    files.append((name, os.path.join(d, name)))
    return sorted(files)


SAMPLES = _collect_samples()
_SAMPLE_PATH = SAMPLES[0][1] if SAMPLES else None
_sample_missing = pytest.mark.skipif(not SAMPLES, reason="无 OFD 样本可用")


# ---------- 构造工具 ----------


def _zip_bytes(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _write(tmp_path, name, data: bytes) -> str:
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


def _set_encrypted_flag(data: bytes) -> bytes:
    """把 zip 所有成员的加密标志位置 1（本地文件头 offset 6、中央目录 offset 8）。"""
    out = bytearray(data)
    i = 0
    while True:
        i = out.find(b"PK\x03\x04", i)
        if i < 0:
            break
        out[i + 6] |= 1
        i += 4
    i = 0
    while True:
        i = out.find(b"PK\x01\x02", i)
        if i < 0:
            break
        out[i + 8] |= 1
        i += 4
    return bytes(out)


def _forge_cd_uncompressed_size(data: bytes, new_size: int = 16) -> bytes:
    """伪造中央目录的解压后大小字段（PK\x01\x02 头 +24 起 4 字节），
    模拟「声明很小、实际解压巨大」的 zip bomb 绕过攻击。"""
    out = bytearray(data)
    i = 0
    while True:
        i = out.find(b"PK\x01\x02", i)
        if i < 0:
            break
        out[i + 24 : i + 28] = new_size.to_bytes(4, "little")
        i += 4
    return bytes(out)


# ---------- 预扫描：validate_ofd_zip ----------


def test_rejects_garbage_with_zip_magic(tmp_path):
    """伪装文件：PK 魔数开头但整体不是合法 zip（穿透上传魔数校验的场景）。"""
    path = _write(tmp_path, "fake.ofd", b"PK\x03\x04" + b"garbage-garbage" * 10)
    with pytest.raises(OfdFileError):
        validate_ofd_zip(path)


def test_rejects_truncated_zip(tmp_path):
    """损坏文件：合法 zip 截断后半段（中央目录丢失）。"""
    data = _zip_bytes({"OFD.xml": b"<of:doc />" * 100, "res/a.xml": b"<r />"})
    path = _write(tmp_path, "broken.ofd", data[: len(data) // 3])
    with pytest.raises(OfdFileError):
        validate_ofd_zip(path)


def test_rejects_empty_zip(tmp_path):
    path = _write(tmp_path, "empty.ofd", _zip_bytes({}))
    with pytest.raises(OfdFileError):
        validate_ofd_zip(path)


def test_rejects_encrypted(tmp_path):
    data = _zip_bytes({"OFD.xml": b"<of:doc />", "res.xml": b"<r />"})
    path = _write(tmp_path, "encrypted.ofd", _set_encrypted_flag(data))
    with pytest.raises(OfdEncryptedError):
        validate_ofd_zip(path)


def test_rejects_entry_count_over_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(ofd_validator, "MAX_ENTRIES", 2)
    data = _zip_bytes({"a.xml": b"1", "b.xml": b"2", "c.xml": b"3"})
    path = _write(tmp_path, "many.ofd", data)
    with pytest.raises(OfdFileError):
        validate_ofd_zip(path)


def test_rejects_total_uncompressed_over_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(ofd_validator, "MAX_TOTAL_UNCOMPRESSED", 1024 * 1024)
    data = _zip_bytes({"bomb.bin": b"\0" * (5 * 1024 * 1024)})
    path = _write(tmp_path, "bomb.ofd", data)
    with pytest.raises(OfdFileError):
        validate_ofd_zip(path)


def test_rejects_zip_bomb_ratio(tmp_path):
    """真实阈值下的压缩比拦截：150MB 全零压缩后仅 ~150KB（比率 ~1000x）。"""
    data = _zip_bytes({"bomb.bin": b"\0" * (150 * 1024 * 1024)})
    path = _write(tmp_path, "bomb.ofd", data)
    with pytest.raises(OfdFileError):
        validate_ofd_zip(path)


def test_rejects_forged_central_directory_size(tmp_path):
    """伪造中央目录声明大小（声明 16B、实际 150MB）：绕过总量/压缩比规则，
    被规则 5（声明+1 字节封顶核对）拦截。"""
    raw = _zip_bytes({"bomb.bin": b"\0" * (150 * 1024 * 1024)})
    data = _forge_cd_uncompressed_size(raw, new_size=16)
    path = _write(tmp_path, "forged.ofd", data)
    with pytest.raises(OfdFileError):
        validate_ofd_zip(path)


def test_rejects_path_traversal(tmp_path):
    data = _zip_bytes({"../evil.txt": b"x", "OFD.xml": b"<of:doc />"})
    path = _write(tmp_path, "slip.ofd", data)
    with pytest.raises(OfdFileError):
        validate_ofd_zip(path)


def test_rejects_absolute_path_entry(tmp_path):
    data = _zip_bytes({"/etc/evil.txt": b"x", "OFD.xml": b"<of:doc />"})
    path = _write(tmp_path, "abs.ofd", data)
    with pytest.raises(OfdFileError):
        validate_ofd_zip(path)


@_sample_missing
def test_accepts_real_ofd_sample():
    validate_ofd_zip(_SAMPLE_PATH)  # 不抛异常即通过


# ---------- 端点：/api/v1/ofd/view 与 /api/v1/pdf/ofd-to-pdf ----------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app.routers import ofd as ofd_router
    from app.routers import pdf as pdf_router
    from app.main import app

    temp_root = str(tmp_path / "temp")
    os.makedirs(temp_root, exist_ok=True)
    monkeypatch.setattr(ofd_router, "TEMP_DIR", temp_root)
    monkeypatch.setattr(pdf_router, "TEMP_DIR", temp_root)
    return TestClient(app)


@_sample_missing
def test_view_endpoint_success(client):
    with open(_SAMPLE_PATH, "rb") as f:
        resp = client.post(
            "/api/v1/ofd/view",
            files={"file": ("sample.ofd", f.read(), "application/ofd")},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # P2-6 协议对齐：TaskResponse 同名字段 + 查看器扩展字段（pdf_url/page_count）
    assert set(body.keys()) == {
        "task_id",
        "page_count",
        "pdf_url",
        "status",
        "message",
        "download_url",
        "file_count",
    }
    assert body["page_count"] >= 1
    assert body["pdf_url"] == f"/api/v1/pdf/download/{body['task_id']}"
    assert body["status"] == "completed"
    assert body["download_url"] == body["pdf_url"]
    assert body["file_count"] == 1


def test_view_endpoint_rejects_fake_file(client):
    resp = client.post(
        "/api/v1/ofd/view",
        files={"file": ("fake.ofd", b"PK\x03\x04" + b"junk" * 32, "application/ofd")},
    )
    assert resp.status_code == 422
    assert "不是有效的 OFD 文件" in resp.json()["detail"]


def test_view_endpoint_rejects_encrypted(client):
    data = _set_encrypted_flag(_zip_bytes({"OFD.xml": b"<of:doc />"}))
    resp = client.post(
        "/api/v1/ofd/view",
        files={"file": ("encrypted.ofd", data, "application/ofd")},
    )
    assert resp.status_code == 422
    assert "加密" in resp.json()["detail"]


def test_view_endpoint_rejects_oversize(client):
    resp = client.post(
        "/api/v1/ofd/view",
        files={"file": ("big.ofd", os.urandom(21 * 1024 * 1024), "application/ofd")},
    )
    assert resp.status_code == 413


@_sample_missing
def test_view_output_page_normalized(client):
    """对抗审查发现：easyofd 对部分样本把 MediaBox 放大 25/9（如 1654pt），
    内容缩在页面左上角。转换管线必须归一化到 ~595pt 宽。"""
    import fitz

    from app.services.ofd_validator import convert_ofd_to_pdf

    import asyncio

    out = os.path.join(os.path.dirname(_SAMPLE_PATH), "_test_norm_out.pdf")
    loop = asyncio.new_event_loop()
    try:
        success, result = loop.run_until_complete(
            convert_ofd_to_pdf(_SAMPLE_PATH, out)
        )
    finally:
        loop.close()
    assert success, result
    doc = fitz.open(out)
    try:
        assert doc[0].rect.width <= 800, f"页面未归一化: {doc[0].rect}"
    finally:
        doc.close()
        try:
            os.remove(out)
        except OSError:
            pass


@_sample_missing
def test_ofd_to_pdf_endpoint_regression(client):
    """加固后的现有端点：真实样本正常转换不回归。"""
    with open(_SAMPLE_PATH, "rb") as f:
        resp = client.post(
            "/api/v1/pdf/ofd-to-pdf",
            files={"file": ("sample.ofd", f.read(), "application/ofd")},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["download_url"].startswith("/api/v1/pdf/download/")


def test_ofd_to_pdf_endpoint_rejects_zip_bomb(client, monkeypatch):
    monkeypatch.setattr(ofd_validator, "MAX_ENTRIES", 2)
    data = _zip_bytes({"a.xml": b"1", "b.xml": b"2", "c.xml": b"3"})
    resp = client.post(
        "/api/v1/pdf/ofd-to-pdf",
        files={"file": ("bomb.ofd", data, "application/ofd")},
    )
    assert resp.status_code == 422
    assert "不是有效的 OFD 文件" in resp.json()["detail"]


def test_convert_ofd_batch_rejects_malicious_ofd(tmp_path):
    """发票合并共享层（统一+旧端点唯一 OFD 转换入口）同样被预扫描覆盖。"""
    from fastapi import HTTPException

    from app.services.invoice_merge_shared import convert_ofd_batch

    bad = _write(tmp_path, "bad.ofd", _zip_bytes({"../evil.txt": b"x", "OFD.xml": b"<of:doc />"}))
    with pytest.raises(HTTPException) as ei:
        convert_ofd_batch(str(tmp_path), [bad], ["bad.ofd"])
    assert ei.value.status_code == 400
    assert "不是有效的 OFD" in ei.value.detail
