"""P2/P3 审计修复回归测试（2026-09-29 全项目挖虫报告第二批）。

逐项锁定修复场景，防止回归。覆盖：
- #17 async 路由内秒级同步重活统一 run_in_threadpool（事件循环阻塞）
- #18 老格式 Office（.doc/.ppt/.xls）显式 415 + word/ppt-to-pdf 保留真实扩展名
- #20 merge 系总量守卫（声明值预检 + merge-batch 落盘实际字节累计）
- #21 台账导出双层预算（单元格 max_length=500 + 路由层 20MB 总字符预算）
- P3 英文错误族（friendly_detail / _friendly_value_detail / pydantic 422 中文化）
- L1 unlock 400 对齐；L2 merge-batch 只数 .pdf；L7 占位符一次性格式化；
  L8 边距×字号组合校验；L9 页眉页脚加密前置；L10 页码格式中文报错；
  L11 压缩级别/0 页拦截；L12 显式纯黑颜色；L13 日期合法性；L14 PhysicalBox；
  L28 OFD 异常前缀剥离；L29 降采样读图；L30 压缩不降反升回传原文件
"""
import inspect
import io
import os
import re

import fitz
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.datastructures import Headers

from app.core.errors import friendly_detail
from app.models.invoice_extract import (
    CELL_MAX_LENGTH,
    MAX_WARNINGS_PER_ROW,
    ExportRowRequest,
)
from app.routers import invoice as invoice_router
from app.routers import pdf as pdf_router
from app.services.invoice_extract.base import normalize_cn_date
from app.services.ofd_converter import _valid_rgb
from app.services.pdf_compressor import PDFCompressor
from app.services.pdf_converter import PDFToMarkdownConverter
from app.services.pdf_header_footer import PDFHeaderFooter


# ---------------------------------------------------------------------------
# 工具：合成 PDF / 加密 PDF / UploadFile
# ---------------------------------------------------------------------------

def _write_pdf(path, n_pages=1):
    doc = fitz.open()
    for _ in range(n_pages):
        page = doc.new_page(width=595.28, height=842)
        page.insert_text((72, 72), "hello pdf")
    doc.save(path)
    doc.close()
    return path


def _write_encrypted_pdf(path, password="pw123"):
    doc = fitz.open()
    page = doc.new_page(width=595.28, height=842)
    page.insert_text((72, 72), "secret content")
    doc.save(
        path, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw=password
    )
    doc.close()
    return path


def _write_empty_pdf(path):
    """0 页 PDF（fitz 拒绝保存无页文档，走 pypdf 构造）。"""
    from pypdf import PdfWriter

    with open(path, "wb") as f:
        PdfWriter().write(f)
    return path


def _upload(data: bytes, filename: str, size=None, content_type="application/pdf"):
    from fastapi import UploadFile

    return UploadFile(
        file=io.BytesIO(data),
        filename=filename,
        size=size,
        headers=Headers({"content-type": content_type}),
    )


# ---------------------------------------------------------------------------
# #17 事件循环阻塞：重活路由必须 async + run_in_threadpool
# ---------------------------------------------------------------------------

class TestEventLoopOffload:
    HEAVY_ROUTES = [
        "split_pdf", "merge_pdf", "merge_batch", "protect_pdf", "unlock_pdf",
        "remove_pages", "rotate_pdf", "watermark_pdf", "organize_pdf",
        "compress_pdf", "add_header_footer", "pdf_to_word", "pdf_to_markdown",
        "pdf_to_jpg", "extract_images", "word_to_pdf", "ppt_to_pdf",
        "word_to_markdown", "ppt_to_markdown", "excel_to_markdown", "jpg_to_pdf",
    ]

    @pytest.mark.parametrize("name", HEAVY_ROUTES)
    def test_route_is_async_and_offloaded(self, name):
        fn = getattr(pdf_router, name)
        assert inspect.iscoroutinefunction(fn), f"{name} 应为 async 路由"
        src = inspect.getsource(fn)
        assert "run_in_threadpool" in src, f"{name} 的同步重活未放线程池"

    def test_invoice_analyze_merge_offloaded(self):
        for name in ("analyze_invoices", "merge_invoices"):
            fn = getattr(invoice_router, name)
            assert inspect.iscoroutinefunction(fn)
            assert "run_in_threadpool" in inspect.getsource(fn)

    def test_ofd_invoice_convert_under_lock_offloaded(self):
        src = inspect.getsource(invoice_router)
        assert "OFD_INVOICE_LOCK" in src
        # 锁内转换必须线程池化（锁住事件循环 = 全站阻塞）
        assert bool(
            re.search(r"with\s+OFD_INVOICE_LOCK[\s\S]{0,600}run_in_threadpool", src)
        )


# ---------------------------------------------------------------------------
# #18 老格式 Office：显式 415 + 真实扩展名落盘
# ---------------------------------------------------------------------------

class TestLegacyOfficeRejection:
    @pytest.mark.parametrize("name", ["old.doc", "old.ppt", "old.xls"])
    def test_legacy_ext_rejected_415(self, name):
        f = _upload(b"", name, 0, "application/msword")
        modern = ".docx" if name.endswith(".doc") else (".pptx" if name.endswith(".ppt") else ".xlsx")
        with pytest.raises(HTTPException) as ei:
            pdf_router._reject_legacy_office(f, modern)
        assert ei.value.status_code == 415
        assert "另存为" in ei.value.detail

    @pytest.mark.parametrize("name", ["new.docx", "new.pptx", "new.xlsx"])
    def test_modern_ext_passes(self, name):
        f = _upload(b"", name, 0, "")
        modern = ".docx" if name.endswith(".docx") else (".pptx" if name.endswith(".pptx") else ".xlsx")
        assert pdf_router._reject_legacy_office(f, modern) is None

    def test_word_ppt_keep_real_extension(self):
        for name in ("word_to_pdf", "ppt_to_pdf"):
            src = inspect.getsource(getattr(pdf_router, name))
            assert 'suffix = Path(file.filename' in src, f"{name} 未保留真实扩展名"
            assert 'f"input{suffix}"' in src


# ---------------------------------------------------------------------------
# #20 merge 系总量守卫
# ---------------------------------------------------------------------------

class TestMergeTotalSizeGuard:
    def test_merge_declared_size_guard(self):
        """两份声明值各超单文件上限 → 声明值预检直接 400（不落盘）。"""
        limit = pdf_router.settings.MAX_UPLOAD_SIZE
        files = [
            _upload(b"", "a.pdf", limit + 1024, "application/pdf"),
            _upload(b"", "b.pdf", limit + 1024, "application/pdf"),
        ]
        import asyncio

        with pytest.raises(HTTPException) as ei:
            asyncio.run(pdf_router.merge_pdf(files=files))
        assert ei.value.status_code == 400
        assert "100MB" in ei.value.detail or "分批" in ei.value.detail

    def test_merge_batch_counts_actual_bytes_only_pdf(self):
        """落盘守卫只累计 .pdf（防 metadata/临时文件计入误判）。"""
        src = inspect.getsource(pdf_router.merge_batch)
        assert "total_written" in src
        assert 'name.endswith(".pdf")' in src or "endswith('.pdf')" in src
        assert "os.path.getsize" in src

    def test_jpg_to_pdf_declared_guard(self):
        limit = pdf_router.settings.MAX_UPLOAD_SIZE
        files = [
            _upload(b"", "a.jpg", limit + 1024, "image/jpeg"),
            _upload(b"", "b.jpg", limit + 1024, "image/jpeg"),
        ]
        import asyncio

        with pytest.raises(HTTPException) as ei:
            asyncio.run(pdf_router.jpg_to_pdf(files=files))
        assert ei.value.status_code == 400


# ---------------------------------------------------------------------------
# #21 台账导出双层预算
# ---------------------------------------------------------------------------

class TestExportRowBudget:
    def test_cell_max_length_enforced(self):
        with pytest.raises(ValidationError):
            ExportRowRequest(invoice_number="A" * (CELL_MAX_LENGTH + 1))

    def test_cell_max_length_ok(self):
        row = ExportRowRequest(invoice_number="A" * CELL_MAX_LENGTH)
        assert row.invoice_number == "A" * CELL_MAX_LENGTH

    def test_warnings_count_limited(self):
        with pytest.raises(ValidationError):
            ExportRowRequest(warnings=["w"] * (MAX_WARNINGS_PER_ROW + 1))

    def test_warning_item_length_limited(self):
        with pytest.raises(ValidationError):
            ExportRowRequest(warnings=["W" * (CELL_MAX_LENGTH + 1)])

    def test_router_total_chars_budget(self):
        src = inspect.getsource(invoice_router_module())
        assert "total_chars" in src
        assert "20_000_000" in src


def invoice_router_module():
    import app.routers.invoice_extract as m

    return m


# ---------------------------------------------------------------------------
# P3 英文错误族：friendly_detail / _friendly_value_detail / 422 中文化
# ---------------------------------------------------------------------------

class TestFriendlyDetail:
    def test_chinese_message_passes_through(self):
        assert friendly_detail(ValueError("发票号码不合法")) == "发票号码不合法"

    def test_ascii_message_replaced(self):
        assert friendly_detail(ValueError("invalid literal for int()")) == "处理失败，请检查文件后重试"

    def test_empty_message_replaced(self):
        assert friendly_detail(ValueError("")) == "处理失败，请检查文件后重试"

    def test_custom_fallback(self):
        assert friendly_detail(ValueError("boom"), "自定义兜底") == "自定义兜底"


class TestFriendlyValueDetail:
    def test_encrypt_keyword_maps_to_guide(self):
        msg = pdf_router._friendly_value_detail(Exception("document closed or encrypted"))
        assert "加密" in msg and "解除" in msg

    def test_english_gives_fallback(self):
        msg = pdf_router._friendly_value_detail(ValueError("invalid literal for int() with base 10"))
        assert msg == "处理失败，请检查文件后重试"

    def test_chinese_business_message_kept(self):
        msg = pdf_router._friendly_value_detail(ValueError("页码区间无效：5-1"))
        assert msg == "页码区间无效：5-1"


class TestValidationErrorMessageI18n:
    def test_missing_translated(self):
        from app.main import _translate_validation_errors

        out = _translate_validation_errors(
            [{"type": "missing", "loc": ("body", "files"), "msg": "Field required", "input": None}]
        )
        assert out[0]["msg"] == "缺少必填参数"

    def test_bool_type_translated(self):
        from app.main import _translate_validation_errors

        out = _translate_validation_errors(
            [{"type": "bool_type", "loc": ("body", "allow_print"), "msg": "Input should be a valid boolean", "input": "yes"}]
        )
        assert "布尔" in out[0]["msg"]

    def test_unknown_type_generic(self):
        from app.main import _translate_validation_errors

        out = _translate_validation_errors(
            [{"type": "some_future_type", "loc": ("body", "x"), "msg": "whatever", "input": None}]
        )
        assert out[0]["msg"]  # 非空即可（通用中文文案）

    def test_non_dict_passthrough(self):
        from app.main import _translate_validation_errors

        assert _translate_validation_errors(["raw"]) == ["raw"]


# ---------------------------------------------------------------------------
# L1 unlock：ValueError → 400（错误码契约对齐）
# ---------------------------------------------------------------------------

class TestUnlockErrorCode:
    def test_wrong_password_is_400(self, tmp_path):
        import asyncio

        enc = _write_encrypted_pdf(str(tmp_path / "enc.pdf"))
        f = _upload(
            open(enc, "rb").read(), "enc.pdf", os.path.getsize(enc), "application/pdf"
        )
        with pytest.raises(HTTPException) as ei:
            asyncio.run(pdf_router.unlock_pdf(file=f, password="wrong-password"))
        assert ei.value.status_code == 400
        assert not ei.value.detail.isascii(), "错误文案不应为库层英文直出"


# ---------------------------------------------------------------------------
# L7 页眉页脚占位符一次性格式化
# ---------------------------------------------------------------------------

class TestRenderVarsSinglePass:
    def _render(self, text, base_name, page_num, total):
        inst = PDFHeaderFooter.__new__(PDFHeaderFooter)  # 跳过文件构造
        return PDFHeaderFooter._render_vars(inst, text, base_name, page_num, total)

    def test_filename_with_placeholders_not_double_replaced(self):
        """文件名本身含 {date}/{page} 时只做一次替换，不发生连锁二次替换。"""
        import datetime

        today = datetime.date.today()
        out = self._render(
            "{filename} {date}", "report_{date}_v{page}.pdf", 3, 10
        )
        assert out == f"report_{{date}}_v{{page}}.pdf {today.strftime('%Y-%m-%d')}"

    def test_all_placeholders_rendered(self):
        out = self._render("{filename}|{year}|{page}/{pages}", "doc.pdf", 2, 7)
        year = out.split("|")[1]
        assert out.startswith("doc.pdf|")
        assert out.endswith("|2/7")
        assert len(year) == 4 and year.isdigit()


# ---------------------------------------------------------------------------
# L8 边距×字号组合校验 / L9 加密前置
# ---------------------------------------------------------------------------

class TestHeaderFooterParamGuards:
    def test_margin_too_small_for_font_rejected(self, tmp_path):
        src_pdf = _write_pdf(str(tmp_path / "in.pdf"))
        with pytest.raises(ValueError, match="边距过小"):
            PDFHeaderFooter(src_pdf).apply(
                str(tmp_path / "out.pdf"),
                header_text="H",
                header_font_size=24,
                margin=20,
                page_number_position="none",
            )

    def test_margin_below_hard_floor_rejected(self, tmp_path):
        src_pdf = _write_pdf(str(tmp_path / "in.pdf"))
        with pytest.raises(ValueError, match="18"):
            PDFHeaderFooter(src_pdf).apply(
                str(tmp_path / "out.pdf"),
                header_text="H",
                margin=10,
                page_number_position="none",
            )

    def test_reasonable_combo_passes(self, tmp_path):
        src_pdf = _write_pdf(str(tmp_path / "in.pdf"))
        info = PDFHeaderFooter(src_pdf).apply(
            str(tmp_path / "out.pdf"),
            header_text="H",
            header_font_size=9,
            margin=36,
            page_number_position="none",
        )
        assert info["headers_applied"] == 1

    def test_encrypted_pdf_rejected_front(self, tmp_path):
        enc = _write_encrypted_pdf(str(tmp_path / "enc.pdf"))
        with pytest.raises(ValueError, match="加密"):
            PDFHeaderFooter(enc).apply(
                str(tmp_path / "out.pdf"), header_text="H",
                page_number_position="none",
            )


# ---------------------------------------------------------------------------
# L10 页码解析中文报错 + 真实页数
# ---------------------------------------------------------------------------

class TestParsePages:
    def test_bad_int_chinese_error(self, tmp_path):
        p = _write_pdf(str(tmp_path / "in.pdf"), 3)
        with pytest.raises(ValueError, match="页码格式无效：a"):
            PDFToMarkdownConverter._parse_pages("1-3,a", p)

    def test_reversed_range_chinese_error(self, tmp_path):
        p = _write_pdf(str(tmp_path / "in.pdf"), 3)
        with pytest.raises(ValueError, match="页码区间无效"):
            PDFToMarkdownConverter._parse_pages("3-1", p)

    def test_huge_range_clamped(self, tmp_path):
        p = _write_pdf(str(tmp_path / "in.pdf"), 3)
        assert PDFToMarkdownConverter._parse_pages("1-99999999", p) == [0, 1, 2]

    def test_valid_mixed(self, tmp_path):
        p = _write_pdf(str(tmp_path / "in.pdf"), 5)
        assert PDFToMarkdownConverter._parse_pages("1-3,5", p) == [0, 1, 2, 4]


# ---------------------------------------------------------------------------
# L11 压缩级别 / 0 页拦截（L30 不降反升回传原文件）
# ---------------------------------------------------------------------------

class TestCompressorGuards:
    def test_invalid_level_chinese(self, tmp_path):
        p = _write_pdf(str(tmp_path / "in.pdf"))
        with pytest.raises(ValueError, match="不支持的压缩级别"):
            PDFCompressor(p).compress(str(tmp_path / "out.pdf"), level="turbo")

    def test_zero_page_pdf_rejected(self, tmp_path):
        p = _write_empty_pdf(str(tmp_path / "empty.pdf"))
        with pytest.raises(ValueError, match="没有任何页面"):
            PDFCompressor(p).compress(str(tmp_path / "out.pdf"), level="normal")

    def test_ineffective_compress_returns_original(self, tmp_path, monkeypatch):
        """L30：压缩无效时输出必须 ≤ 原文件（回传原文件内容）。"""
        p = _write_pdf(str(tmp_path / "in.pdf"))
        out = str(tmp_path / "out.pdf")
        in_size = os.path.getsize(p)
        compressor = PDFCompressor(p)
        monkeypatch.setattr(compressor, "_find_gs", lambda: None)  # 强制 fallback
        compressor.compress(out, level="normal")
        assert os.path.exists(out)
        assert os.path.getsize(out) <= in_size


# ---------------------------------------------------------------------------
# L12 显式纯黑 (0,0,0) 不再被误判为缺色
# ---------------------------------------------------------------------------

class TestValidRgb:
    def test_explicit_black_is_valid(self):
        """L12 核心：显式纯黑 (0,0,0) 必须返回真值 tuple，不得判为缺色。"""
        assert _valid_rgb((0, 0, 0)) == (0, 0, 0)
        assert _valid_rgb([0, 0, 0]) == (0, 0, 0)

    def test_float_values_clamped_to_int_tuple(self):
        assert _valid_rgb([128.7, 255.0, 10.2]) == (128, 255, 10)
        assert _valid_rgb([-5, 300, 10]) == (0, 255, 10)  # 截断到 0-255

    def test_extra_values_take_first_three(self):
        assert _valid_rgb([10, 20, 30, 40]) == (10, 20, 30)

    @pytest.mark.parametrize("bad", [None, [], [0, 0], "rgb", ["a", "b", "c"]])
    def test_invalid_returns_none(self, bad):
        assert _valid_rgb(bad) is None


# ---------------------------------------------------------------------------
# L13 日期合法性（2026-13-40 不再进台账）
# ---------------------------------------------------------------------------

class TestNormalizeCnDate:
    def test_cn_format(self):
        assert normalize_cn_date("2026年08月25日") == "2026-08-25"

    def test_dash_format_normalized(self):
        assert normalize_cn_date("2026-8-5") == "2026-08-05"

    def test_invalid_month_day_returns_none(self):
        assert normalize_cn_date("2026-13-40") is None
        assert normalize_cn_date("2026年13月40日") is None

    def test_feb_30_returns_none(self):
        assert normalize_cn_date("2026-02-30") is None

    def test_garbage_and_none(self):
        assert normalize_cn_date("not a date") is None
        assert normalize_cn_date(None) is None
        assert normalize_cn_date("") is None


# ---------------------------------------------------------------------------
# L14 OFD PhysicalBox 非数值 → 带真实原因的 InvoiceExtractError（源码级锁定）
# ---------------------------------------------------------------------------

class TestOfdPhysicalBoxGuard:
    def test_physical_box_wrapped_as_extract_error(self):
        import app.services.invoice_extract.ofd_adapter as m

        src = inspect.getsource(m)
        assert "PhysicalBox" in src
        assert "页面尺寸数据无效" in src


# ---------------------------------------------------------------------------
# L28 OFD 转换失败：异常类名前缀剥离 + 纯 ASCII 不直出
# ---------------------------------------------------------------------------

class TestOfdToPdfErrorScrub:
    def test_ofd_to_pdf_scrubs_error_prefix(self):
        src = inspect.getsource(pdf_router.ofd_to_pdf)
        assert "Assertion" in src or "Error|Exception" in src
        assert "isascii" in src


# ---------------------------------------------------------------------------
# L29 OCR 模糊检测降采样读图（源码级锁定）
# ---------------------------------------------------------------------------

class TestPrecheckDownsample:
    def test_precheck_uses_reduced_read(self):
        import app.services.ocr.precheck as m

        src = inspect.getsource(m)
        assert "IMREAD_REDUCED_COLOR_2" in src


# ---------------------------------------------------------------------------
# #17 补充：splitter 中文文案（防英文库层错误回归）
# ---------------------------------------------------------------------------

class TestSplitterMessages:
    def test_invalid_mode_chinese(self, tmp_path):
        from app.services.pdf_splitter import PDFSplitter

        p = _write_pdf(str(tmp_path / "in.pdf"), 2)
        with pytest.raises(ValueError, match="不支持的拆分模式"):
            PDFSplitter(p).split(mode="bogus", value="", output_dir=str(tmp_path))
