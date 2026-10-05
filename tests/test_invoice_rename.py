"""发票批量重命名测试（设计文档 docs/2026-09-30_100232-invoice-batch-rename-design.md §8）。

- 纯函数：命名拼装 / 文件名安全清洗 / 去重（§8.1）；
- 路由集成（TestClient）：混合批次 / 重名 / 注入清洗 / 限制 / 下载鉴权（§8.2）。
夹具复用 test_invoice_extract 的数电票 XML/PDF 构造器。
"""
import io
import uuid as _uuid
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.invoice_extract import InvoiceRecord
from app.services.invoice_rename import (
    MAX_BUYER_LEN,
    MAX_FILENAME_LEN,
    MAX_PREFIX_LEN,
    _sanitize_component,
    build_report_text,
    build_target_name,
    dedup_names,
)
from tests.test_invoice_extract import _make_pdf_bytes, _xml

client = TestClient(app)

# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------

NUM_XML = "26112000001182262471"
BUYER_XML = "北京创信卓远信息技术有限责任公司"
DATE_XML = "2026-03-26"

NUM_PDF = "26112000003559581871"
BUYER_PDF = "中国人民财产保险股份有限公司"
DATE_PDF = "2026-08-25"


def _rec(**kw) -> InvoiceRecord:
    defaults = dict(
        invoice_number=NUM_PDF,
        buyer_name=BUYER_PDF,
        issue_date=DATE_PDF,
        total_with_tax="736792.00",
    )
    defaults.update(kw)
    return InvoiceRecord(**defaults)


def _fu(data: bytes, name: str, mime: str = "application/octet-stream"):
    """TestClient 上传元组（字段名固定 files）。"""
    return ("files", (name, data, mime))


# ---------------------------------------------------------------------------
# §8.1 纯函数：命名拼装
# ---------------------------------------------------------------------------

class TestBuildTargetName:
    def test_full_fields(self):
        name = build_target_name(_rec(), ".pdf")
        assert name == f"{NUM_PDF}_{BUYER_PDF}_{DATE_PDF}.pdf"

    def test_empty_prefix_omitted(self):
        name = build_target_name(_rec(), ".pdf", "")
        assert name == f"{NUM_PDF}_{BUYER_PDF}_{DATE_PDF}.pdf"

    def test_with_prefix(self):
        name = build_target_name(_rec(), ".pdf", "2026年9月报销-")
        assert name == f"2026年9月报销-{NUM_PDF}_{BUYER_PDF}_{DATE_PDF}.pdf"

    def test_include_amount(self):
        name = build_target_name(_rec(), ".pdf", include_amount=True)
        assert name == f"{NUM_PDF}_{BUYER_PDF}_{DATE_PDF}_736792.00元.pdf"

    def test_amount_off_by_default_and_when_total_missing(self):
        assert "_元" not in build_target_name(_rec(), ".pdf")
        rec = _rec(total_with_tax=None)
        assert "_元" not in build_target_name(rec, ".pdf", include_amount=True)

    def test_buyer_truncated_to_30(self):
        long_buyer = "北" * 40
        name = build_target_name(_rec(buyer_name=long_buyer), ".pdf")
        assert f"{'北' * MAX_BUYER_LEN}_" in name
        assert long_buyer not in name

    def test_extension_preserved(self):
        assert build_target_name(_rec(), ".ofd").endswith(".ofd")
        assert build_target_name(_rec(), ".xml").endswith(".xml")

    def test_include_type_special(self):
        """票种开关：增值税专用发票 → _专票。"""
        rec = _rec(invoice_type="电子发票（增值税专用发票）")
        name = build_target_name(rec, ".pdf", include_type=True)
        assert name == f"{NUM_PDF}_{BUYER_PDF}_{DATE_PDF}_专票.pdf"

    def test_include_type_normal(self):
        rec = _rec(invoice_type="电子发票（普通发票）")
        name = build_target_name(rec, ".pdf", include_type=True)
        assert name == f"{NUM_PDF}_{BUYER_PDF}_{DATE_PDF}_普票.pdf"

    def test_include_type_unknown_omitted(self):
        """类型缺失/无法判定 → 不加段（宁缺勿错），其余不变。"""
        no_type = build_target_name(_rec(invoice_type=None), ".pdf", include_type=True)
        assert no_type == f"{NUM_PDF}_{BUYER_PDF}_{DATE_PDF}.pdf"
        other = build_target_name(
            _rec(invoice_type="电子发票（机票）"), ".pdf", include_type=True
        )
        assert other == f"{NUM_PDF}_{BUYER_PDF}_{DATE_PDF}.pdf"

    def test_type_off_by_default(self):
        name = build_target_name(_rec(invoice_type="电子发票（增值税专用发票）"), ".pdf")
        assert "专票" not in name

    def test_type_and_amount_order(self):
        """票种在日期后、金额保持末尾。"""
        rec = _rec(invoice_type="电子发票（增值税专用发票）")
        name = build_target_name(rec, ".pdf", include_type=True, include_amount=True)
        assert name == f"{NUM_PDF}_{BUYER_PDF}_{DATE_PDF}_专票_736792.00元.pdf"

    def test_missing_buyer_and_date_fallbacks(self):
        rec = _rec(buyer_name=None, issue_date=None)
        name = build_target_name(rec, ".pdf")
        assert name == f"{NUM_PDF}_unknown.pdf"

    def test_prefix_truncated_first_when_total_exceeds(self):
        long_prefix = "前" * MAX_PREFIX_LEN
        name = build_target_name(_rec(), ".pdf", long_prefix)
        assert len(name) <= MAX_FILENAME_LEN
        # 名字主体（无前缀部分）完整保留
        assert name.endswith(f"{NUM_PDF}_{BUYER_PDF}_{DATE_PDF}.pdf")


# ---------------------------------------------------------------------------
# §8.1 纯函数：文件名安全清洗（§4.2）
# ---------------------------------------------------------------------------

class TestSanitizeComponent:
    def test_illegal_chars_replaced(self):
        out = _sanitize_component('a<b>:c"d/e\\f|g?h*i', 100)
        assert out == "a_b__c_d_e_f_g_h_i"
        for ch in '<>:"/\\|?*':
            assert ch not in out

    def test_control_chars_replaced(self):
        assert "\x00" not in _sanitize_component("a\x00b\x1fc", 100)

    def test_traversal_dotdot_replaced(self):
        out = _sanitize_component("../../etc/passwd", 100)
        assert ".." not in out
        assert "/" not in out

    def test_leading_trailing_dots_spaces_stripped(self):
        assert _sanitize_component("  name. ", 100) == "name"
        assert _sanitize_component("...", 100) == "_"
        assert _sanitize_component("", 100) == ""
        assert _sanitize_component(None, 100) == ""

    @pytest.mark.parametrize("reserved", ["CON", "con", "NUL", "Lpt1", "COM3"])
    def test_windows_reserved_prefixed(self, reserved):
        assert _sanitize_component(reserved, 100).startswith("_")

    def test_windows_reserved_with_extension(self):
        assert _sanitize_component("CON.pdf", 100) == "_CON.pdf"

    def test_truncation_and_post_truncation_cleanup(self):
        # 截断后尾部恰好是点号 → 再 strip
        out = _sanitize_component("ab." + "c" * 50, 4)
        assert len(out) <= 4 and not out.endswith(".")

    def test_truncated_reserved_rechecked(self):
        # 截断后重新拼出保留名 → 兜底加前缀（前缀致超长 1 字符可接受，安全优先）
        assert _sanitize_component("CONSOLE", 3) == "_CON"


# ---------------------------------------------------------------------------
# §8.1 纯函数：去重（§4.3）
# ---------------------------------------------------------------------------

class TestDedupNames:
    def test_three_duplicates(self):
        out, resolved = dedup_names(["a.pdf", "a.pdf", "a.pdf"])
        assert out == ["a.pdf", "a__2.pdf", "a__3.pdf"]
        assert resolved == 2

    def test_different_ext_not_duplicate(self):
        out, resolved = dedup_names(["a.pdf", "a.ofd"])
        assert out == ["a.pdf", "a.ofd"]
        assert resolved == 0

    def test_mixed(self):
        out, resolved = dedup_names(["x.pdf", "y.pdf", "x.pdf"])
        assert out == ["x.pdf", "y.pdf", "x__2.pdf"]
        assert resolved == 1

    def test_existing_serial_collision(self):
        # 原名单里已有 a__2.pdf → 新重复跳到 __3（仅 1 个重复文件，resolved=1）
        out, resolved = dedup_names(["a__2.pdf", "a.pdf", "a.pdf"])
        assert "a__3.pdf" in out
        assert resolved == 1

    def test_no_ext_duplicate(self):
        out, _ = dedup_names(["README", "README"])
        assert out == ["README", "README__2"]


# ---------------------------------------------------------------------------
# 报告文本
# ---------------------------------------------------------------------------

class TestReportText:
    def test_contains_sections(self):
        text = build_report_text(
            [("a.pdf", "26120..._x_2026-01-01.pdf")],
            [("bad.pdf", "未能识别出发票号码")],
        )
        assert "成功重命名：1" in text and "未识别（保留原名）：1" in text
        assert "a.pdf → 26120..._x_2026-01-01.pdf" in text
        assert "bad.pdf：未能识别出发票号码" in text

    def test_all_skipped_hint(self):
        text = build_report_text([], [("x.pdf", "reason")])
        assert "按原文件名" in text and "仅支持数电票" in text


# ---------------------------------------------------------------------------
# §8.2 路由集成
# ---------------------------------------------------------------------------

class TestRenameBatchRoute:
    def test_mixed_batch(self):
        """2 XML + 1 数电票 PDF + 1 伪装文件 → renamed=3, skipped=1。"""
        files = [
            _fu(_xml(), "a.xml", "application/xml"),
            _fu(_xml(number="26112000001182262472", date="2026-03-01"),
                "b.xml", "application/xml"),
            _fu(_make_pdf_bytes(), "c.pdf", "application/pdf"),
            _fu(b"definitely not a pdf", "fake.pdf", "application/pdf"),
        ]
        r = client.post(
            "/api/v1/invoice/rename-batch",
            files=files,
            data={"prefix": "", "include_amount": "false"},
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total"] == 4
        assert body["renamed"] == 3
        assert body["duplicates_resolved"] == 0
        assert len(body["skipped"]) == 1
        assert body["skipped"][0]["file"] == "fake.pdf"

        dl = client.get(body["download_url"])
        assert dl.status_code == 200
        assert dl.headers["content-type"].startswith("application/zip")
        zf = zipfile.ZipFile(io.BytesIO(dl.content))
        names = zf.namelist()
        assert f"{NUM_XML}_{BUYER_XML}_{DATE_XML}.xml" in names
        assert (
            f"26112000001182262472_{BUYER_XML}_2026-03-01.xml" in names
        )
        assert f"{NUM_PDF}_{BUYER_PDF}_{DATE_PDF}.pdf" in names
        assert "fake.pdf" in names  # 未识别保留原名
        assert "_重命名报告.txt" in names
        report = zf.read("_重命名报告.txt").decode("utf-8-sig")
        assert "成功重命名：3" in report and "未识别（保留原名）：1" in report
        assert "fake.pdf" in report

    def test_prefix_and_amount_end_to_end(self):
        r = client.post(
            "/api/v1/invoice/rename-batch",
            files=[_fu(_xml(), "a.xml", "application/xml")],
            data={"prefix": "报销-", "include_amount": "true"},
        )
        body = r.json()
        dl = client.get(body["download_url"])
        names = zipfile.ZipFile(io.BytesIO(dl.content)).namelist()
        assert (
            f"报销-{NUM_XML}_{BUYER_XML}_{DATE_XML}_270.00元.xml" in names
        )

    def test_include_type_end_to_end(self):
        """票种开关端到端：普通发票 → _普票；专用发票 → _专票。"""
        files = [
            _fu(_xml(), "a.xml", "application/xml"),
            _fu(_xml(number="26112000001182262488", vat="增值税专用发票"),
                "b.xml", "application/xml"),
        ]
        r = client.post(
            "/api/v1/invoice/rename-batch",
            files=files,
            data={"include_type": "true"},
        )
        body = r.json()
        assert body["renamed"] == 2
        dl = client.get(body["download_url"])
        names = zipfile.ZipFile(io.BytesIO(dl.content)).namelist()
        assert f"{NUM_XML}_{BUYER_XML}_{DATE_XML}_普票.xml" in names
        assert (
            f"26112000001182262488_{BUYER_XML}_2026-03-26_专票.xml" in names
        )

    def test_duplicates_resolved(self):
        """同号码同购买方同日期（红冲重开）→ 第二个自动 __2。"""
        files = [
            _fu(_xml(), "a.xml", "application/xml"),
            _fu(_xml(), "b.xml", "application/xml"),
        ]
        body = client.post("/api/v1/invoice/rename-batch", files=files).json()
        assert body["renamed"] == 2
        assert body["duplicates_resolved"] == 1
        dl = client.get(body["download_url"])
        names = zipfile.ZipFile(io.BytesIO(dl.content)).namelist()
        assert f"{NUM_XML}_{BUYER_XML}_{DATE_XML}.xml" in names
        assert f"{NUM_XML}_{BUYER_XML}_{DATE_XML}__2.xml" in names

    def test_injection_sanitized(self):
        """购买方名称含 ../../evil<> → 目标名已清洗、无路径穿越。"""
        # XML 文本节点中 < > 需实体转义
        evil_xml = _xml(buyer_name="A&lt;&gt;B/../../C")
        r = client.post(
            "/api/v1/invoice/rename-batch",
            files=[_fu(evil_xml, "evil.xml", "application/xml")],
        )
        assert r.status_code == 200
        body = r.json()
        assert body["renamed"] == 1
        dl = client.get(body["download_url"])
        zf = zipfile.ZipFile(io.BytesIO(dl.content))
        for name in zf.namelist():
            assert not name.startswith("/")
            assert ".." not in name
            assert "/" not in name  # 平铺结构，不允许子目录
            assert "<" not in name and ">" not in name
        assert any(name.startswith(NUM_XML) for name in zf.namelist())

    def test_unsupported_type_keeps_original_name(self):
        r = client.post(
            "/api/v1/invoice/rename-batch",
            files=[
                _fu(b"\xff\xd8\xff fake jpg", "IMG_2031.jpg", "image/jpeg"),
                _fu(_xml(), "a.xml", "application/xml"),
            ],
        )
        body = r.json()
        assert body["renamed"] == 1
        assert len(body["skipped"]) == 1
        assert body["skipped"][0]["file"] == "IMG_2031.jpg"
        dl = client.get(body["download_url"])
        names = zipfile.ZipFile(io.BytesIO(dl.content)).namelist()
        assert "IMG_2031.jpg" in names

    def test_encrypted_ofd_skipped(self):
        """加密 OFD → skipped 且保留原名（预扫描生效）。"""
        # validate_ofd_zip 的加密检测读中央目录 flag_bits & 0x1（zipfile
        # infolist 来源），本地头 + 中央目录两处都置位以模拟真实加密 ZIP：
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("OFD.xml", "<ofd:OFDDoc/>")
        data = bytearray(buf.getvalue())
        pos = 0
        while True:
            pos = data.find(b"PK\x03\x04", pos)
            if pos == -1:
                break
            data[pos + 6] |= 0x01  # 本地文件头 general purpose flag
            pos += 4
        pos = 0
        while True:
            pos = data.find(b"PK\x01\x02", pos)
            if pos == -1:
                break
            data[pos + 8] |= 0x01  # 中央目录 general purpose flag
            pos += 4
        r = client.post(
            "/api/v1/invoice/rename-batch",
            files=[_fu(bytes(data), "enc.ofd", "application/ofd")],
        )
        body = r.json()
        assert body["renamed"] == 0
        assert body["skipped"][0]["file"] == "enc.ofd"
        assert "加密" in body["skipped"][0]["reason"]

    def test_51_files_rejected(self):
        files = [_fu(b"<x/>", f"f{i}.xml", "application/xml") for i in range(51)]
        r = client.post("/api/v1/invoice/rename-batch", files=files)
        assert r.status_code == 400
        assert "50" in r.json()["detail"]

    def test_oversized_file_413(self):
        big = b"a" * (10 * 1024 * 1024 + 1)
        r = client.post(
            "/api/v1/invoice/rename-batch",
            files=[_fu(big, "big.xml", "application/xml")],
        )
        assert r.status_code == 413

    def test_prefix_too_long_400(self):
        r = client.post(
            "/api/v1/invoice/rename-batch",
            files=[_fu(_xml(), "a.xml", "application/xml")],
            data={"prefix": "长" * 51},
        )
        assert r.status_code == 400

    def test_empty_files_400(self):
        r = client.post("/api/v1/invoice/rename-batch", files=[])
        assert r.status_code in (400, 422)


# ---------------------------------------------------------------------------
# 审查①（安全与攻击面）回归
# ---------------------------------------------------------------------------

class TestReview1Security:
    def test_invisible_chars_stripped(self):
        """零宽/双向控制字符（RTLO 视觉伪装）拼名前剥离。"""
        evil = "a\u200bb\u202ec.pdf"
        out = _sanitize_component(evil, 50)
        for ch in "\u200b\u200e\u202a\u202e\u2066\ufeff":
            assert ch not in out

    def test_total_size_guard(self, monkeypatch):
        """总量守卫：实读累计超上限 → 400 整批拒绝（不落任务产物）。"""
        from app.routers import invoice_rename as mod

        monkeypatch.setattr(mod, "MAX_TOTAL_SIZE", 1024)
        files = [
            _fu(b"x" * 700, "a.xml", "application/xml"),
            _fu(b"y" * 700, "b.xml", "application/xml"),
        ]
        r = client.post("/api/v1/invoice/rename-batch", files=files)
        assert r.status_code == 400
        assert "100MB" in r.json()["detail"] or "分批" in r.json()["detail"]

    def test_skipped_arcname_collisions(self):
        """skipped 之间/与报告名撞名 → 自动加序号，ZIP 条目名唯一。"""
        report_file = "_重命名报告.txt"
        files = [
            _fu(b"not a pdf", "fake.pdf", "application/pdf"),
            _fu(b"not a pdf", "fake.pdf", "application/pdf"),
            _fu(b"junk", report_file, "text/plain"),
        ]
        body = client.post("/api/v1/invoice/rename-batch", files=files).json()
        assert body["renamed"] == 0
        assert len(body["skipped"]) == 3
        dl = client.get(body["download_url"])
        zf = zipfile.ZipFile(io.BytesIO(dl.content))
        names = zf.namelist()
        # ZIP 条目名无重复（重复条目解压时后者覆盖前者 → 静默丢文件）
        assert len(names) == len(set(names))
        assert report_file in names  # 报告本体完好
        assert "_重命名报告__2.txt" in names  # 撞名的用户文件让位
        assert "fake.pdf" in names and "fake__2.pdf" in names
        # 撞名文件内容各自完好
        assert zf.read("fake.pdf") == b"not a pdf"
        assert zf.read("fake__2.pdf") == b"not a pdf"

    def test_encrypted_pdf_clear_message(self):
        """口令保护 PDF → 明确"已加密"提示，不再落入"解析失败"误导文案。"""
        from pypdf import PdfReader, PdfWriter

        writer = PdfWriter()
        for page in PdfReader(io.BytesIO(_make_pdf_bytes())).pages:
            writer.add_page(page)
        writer.encrypt("secret")
        buf = io.BytesIO()
        writer.write(buf)
        body = client.post(
            "/api/v1/invoice/rename-batch",
            files=[_fu(buf.getvalue(), "locked.pdf", "application/pdf")],
        ).json()
        assert body["renamed"] == 0
        assert "加密" in body["skipped"][0]["reason"]


class TestRenameDownload:
    def test_invalid_task_id_400(self):
        r = client.get("/api/v1/invoice/rename-download/not-a-uuid")
        assert r.status_code == 400

    def test_missing_task_404(self):
        r = client.get(f"/api/v1/invoice/rename-download/{_uuid.uuid4()}")
        assert r.status_code == 404

    def test_full_flow_download_name(self):
        r = client.post(
            "/api/v1/invoice/rename-batch",
            files=[_fu(_xml(), "a.xml", "application/xml")],
        )
        task_id = r.json()["task_id"]
        dl = client.get(f"/api/v1/invoice/rename-download/{task_id}")
        assert dl.status_code == 200
        assert dl.headers["content-disposition"].startswith("attachment")
        assert f"invoice_renamed_{task_id[:8]}.zip" in dl.headers.get(
            "content-disposition", ""
        )
