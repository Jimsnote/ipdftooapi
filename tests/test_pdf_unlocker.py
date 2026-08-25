"""PDFUnlocker 纯逻辑测试：知道密码 / 仅权限密码 / 错误密码 / 未加密 四场景。"""

import io
import os
import tempfile

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.constants import UserAccessPermissions as UAP

from app.services.pdf_unlocker import PDFUnlocker


def _blank_pdf() -> bytes:
    buf = io.BytesIO()
    w = PdfWriter()
    w.add_blank_page(width=595.27, height=841.89)
    w.write(buf)
    return buf.getvalue()


def _protected_pdf(user_pwd: str, owner_pwd: str | None = None, restrict: bool = False) -> bytes:
    w = PdfWriter()
    w.append(io.BytesIO(_blank_pdf()))
    perms = UAP.PRINT if restrict else UAP.PRINT | UAP.MODIFY | UAP.EXTRACT
    w.encrypt(
        user_password=user_pwd,
        owner_password=owner_pwd or user_pwd,
        use_128bit=True,
        permissions_flag=perms,
    )
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _run(content: bytes, password: str = "") -> dict:
    d = tempfile.mkdtemp()
    src = os.path.join(d, "src.pdf")
    out = os.path.join(d, "unlocked.pdf")
    with open(src, "wb") as f:
        f.write(content)
    result = PDFUnlocker(src).unlock(out, password=password)
    return result, out


def test_unlock_with_correct_user_password():
    (result, out) = _run(_protected_pdf("mypass123"), password="mypass123")
    assert result["was_encrypted"] is True
    assert PdfReader(out).is_encrypted is False
    assert len(PdfReader(out).pages) == 1


def test_unlock_owner_password_only_with_empty_password():
    # 仅权限保护（打开密码为空）：无需密码即可解除
    (result, out) = _run(_protected_pdf("", owner_pwd="ownersecret", restrict=True))
    assert result["was_encrypted"] is True
    assert PdfReader(out).is_encrypted is False


def test_unlock_wrong_password_raises():
    with pytest.raises(ValueError, match="密码不正确"):
        _run(_protected_pdf("real123"), password="wrongpass")


def test_unlock_not_encrypted_passthrough():
    raw = _blank_pdf()
    (result, out) = _run(raw)
    assert result["was_encrypted"] is False
    with open(out, "rb") as f:
        assert f.read() == raw
