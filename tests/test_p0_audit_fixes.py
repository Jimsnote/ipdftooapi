"""P0 审计修复回归测试（2026-09-29 全项目挖虫报告 #1/#2/#3/#4/#5/#6/#7/#14）。

逐项锁定"修复前实测可复现"的场景，防止回归：
- #1  发票合并 OFD/PDF 混合序列编号覆盖（[OFD, PDF, OFD]）
- #2  文字平铺水印超大页面锚点爆炸（20 万 pt 页 → 174 万次 insert_text）
- #3  渲染炸弹家族（to-jpg 像素预算 / fill 长宽比 / 解压炸弹 / 发票合并页数上限）
- #4  发票合并 per_page 非法值（0 除零 / 负数假成功 / 7、999 静默兜底）
- #5  台账导出公式注入（= + - @ 前缀）
- #6  台账提取多页 PDF 跨页串扰
- #7  PDF 分割 ranges 无上限（25 万+ 文件）
- #14 PDF 转 Word 页码 0 基（pages="1" 静默转第 2 页）
"""
import io
import os

import fitz
import pytest
from PIL import Image as PILImage

from app.services.image_to_pdf import ImageToPDFConverter
from app.services.invoice_extract import InvoiceExtractError, export_csv, export_xlsx
from app.services.invoice_extract.exporter import _sanitize_cell
from app.services.invoice_extract.pdf_adapter import extract_pdf_bytes
from app.services.invoice_merge_shared import convert_ofd_batch
from app.services.invoice_merger import InvoiceMerger
from app.models.invoice_extract import ExportRowRequest
from app.services.pdf_splitter import PDFSplitter
from app.services.pdf_to_image import PDFToImageConverter
from app.services.pdf_to_word import PDFToWordConverter
from app.services.pdf_watermarker import MAX_TILE_ANCHORS, PDFWatermarker
from app.services.render_budget import (
    ensure_image_pixel_limit,
    ensure_page_pixel_budget,
)


# ---------------------------------------------------------------------------
# 工具：合成 PDF / PNG
# ---------------------------------------------------------------------------

def _write_pdf(path, n_pages=1, width=595.28, height=842):
    doc = fitz.open()
    for _ in range(n_pages):
        doc.new_page(width=width, height=height)
    doc.save(path)
    doc.close()
    return path


# ---------------------------------------------------------------------------
# #1 发票合并：OFD 产物按全局序号编号
# ---------------------------------------------------------------------------

class TestOfdBatchNumbering:
    def _run(self, tmp_path, monkeypatch, items):
        import app.services.invoice_merge_shared as shared

        class FakeConverter:
            def ofd_to_pdf(self, src, dst):
                with open(dst, "wb") as f:
                    f.write(b"%PDF-1.4 stub\n")
                return True, "ok"

        monkeypatch.setattr(shared, "OFDConverter", FakeConverter)
        monkeypatch.setattr(shared, "validate_ofd_zip", lambda p: None)

        def fake_normalize(raw, final):
            with open(final, "wb") as f:
                f.write(b"%PDF-1.4 normalized\n")

        monkeypatch.setattr(shared, "normalize_pdf", fake_normalize)
        return shared.convert_ofd_batch(str(tmp_path), items, [f"n{i}.ofd" for i, _ in items])

    def test_global_index_numbering_no_overwrite(self, tmp_path, monkeypatch):
        """[OFD@0, OFD@2]：第二个 OFD 产物必须是 invoice_003.pdf，
        旧连续编号（start_index+1, +2 → invoice_002）会覆盖 PDF 直存的 002。"""
        out = self._run(tmp_path, monkeypatch, [(0, "a.ofd"), (2, "c.ofd")])
        names = sorted(os.path.basename(p) for p in out)
        assert names == ["invoice_001.pdf", "invoice_003.pdf"]
        for p in out:
            assert os.path.exists(p)

    def test_sparse_global_indices(self, tmp_path, monkeypatch):
        """[OFD@1, OFD@3, OFD@4]（OFD OFD PDF OFD OFD 混合）：编号 002/004/005，
        与 PDF 直存的 001/003 命名空间对齐、互不冲突。"""
        out = self._run(tmp_path, monkeypatch, [(1, "b.ofd"), (3, "d.ofd"), (4, "e.ofd")])
        names = sorted(os.path.basename(p) for p in out)
        assert names == ["invoice_002.pdf", "invoice_004.pdf", "invoice_005.pdf"]


# ---------------------------------------------------------------------------
# #2 水印 tile 锚点上限
# ---------------------------------------------------------------------------

class TestWatermarkAnchorCap:
    def test_huge_page_rejected(self):
        with pytest.raises(ValueError) as ei:
            PDFWatermarker._anchor_points(
                fitz.Rect(0, 0, 200000, 200000), "tile", 100.0, 48.0
            )
        assert "过大" in str(ei.value)

    def test_normal_page_bounded(self):
        pts = PDFWatermarker._anchor_points(fitz.Rect(0, 0, 595, 842), "tile", 200.0, 48.0)
        assert 0 < len(pts) <= MAX_TILE_ANCHORS

    def test_center_layout_unaffected(self):
        pts = PDFWatermarker._anchor_points(
            fitz.Rect(0, 0, 200000, 200000), "center", 100.0, 48.0
        )
        assert len(pts) == 1

    def test_huge_page_rejected_via_service(self, tmp_path):
        """端到端：watermark_text 在渲染前抛 ValueError（路由层映射 400），不再挂死。"""
        pdf = _write_pdf(str(tmp_path / "big.pdf"), width=200000, height=200000)
        wm = PDFWatermarker(pdf)
        out = str(tmp_path / "out.pdf")
        with pytest.raises(ValueError) as ei:
            wm.watermark_text("机密", out, layout="tile")
        assert "过大" in str(ei.value)
        assert not os.path.exists(out)


# ---------------------------------------------------------------------------
# #3 渲染炸弹家族
# ---------------------------------------------------------------------------

class TestRenderBudget:
    def test_page_budget_normal_a4_ok(self):
        ensure_page_pixel_budget(595.28, 841.89, 300)

    def test_page_budget_huge_rejected(self):
        with pytest.raises(ValueError):
            ensure_page_pixel_budget(20000, 20000, 150)
        with pytest.raises(ValueError):
            ensure_page_pixel_budget(5000, 5000, 300)  # 20833px 边超限

    def test_image_pixel_limit(self):
        ensure_image_pixel_limit(4000, 3000)
        with pytest.raises(ValueError):
            ensure_image_pixel_limit(13000, 13000)  # 1.69 亿像素解压炸弹
        with pytest.raises(ValueError):
            ensure_image_pixel_limit(9000, 9000)  # 8100 万像素 > 67M 预算


class TestToImagePixelBudget:
    def test_huge_page_rejected_before_render(self, tmp_path):
        pdf = _write_pdf(str(tmp_path / "big.pdf"), width=20000, height=20000)
        conv = PDFToImageConverter(pdf)
        with pytest.raises(ValueError) as ei:
            conv.convert(str(tmp_path), format="jpg", dpi=150)
        assert "过大" in str(ei.value)


class TestImageToPdfGuards:
    def test_fill_mode_extreme_aspect_rejected(self, tmp_path):
        """审计 #3b：1×50000 图片 fill 模式曾把 resize 目标撑到 6200 万像素。"""
        img_path = str(tmp_path / "pano.png")
        PILImage.new("RGB", (50000, 1), "red").save(img_path)
        with pytest.raises(ValueError) as ei:
            ImageToPDFConverter.convert(
                [img_path], str(tmp_path / "out.pdf"), fit_mode="fill"
            )
        assert "长宽比" in str(ei.value)

    def test_fill_mode_normal_aspect_ok(self, tmp_path):
        img_path = str(tmp_path / "normal.png")
        PILImage.new("RGB", (800, 600), "red").save(img_path)
        out = ImageToPDFConverter.convert(
            [img_path], str(tmp_path / "out.pdf"), fit_mode="fill"
        )
        assert os.path.exists(out)


class TestInvoiceMergePageCap:
    def test_total_pages_over_cap_rejected(self, tmp_path):
        """审计 #3e：60+50=110 页 > 100 页上限，渲染前显式拒绝。"""
        a = _write_pdf(str(tmp_path / "a.pdf"), n_pages=60)
        b = _write_pdf(str(tmp_path / "b.pdf"), n_pages=50)
        merger = InvoiceMerger()
        merger.analyze([a, b])
        with pytest.raises(ValueError) as ei:
            merger.merge(str(tmp_path / "out.pdf"), per_page=1)
        assert "超过上限" in str(ei.value)

    def test_within_cap_ok(self, tmp_path):
        a = _write_pdf(str(tmp_path / "a.pdf"), n_pages=2)
        merger = InvoiceMerger()
        merger.analyze([a])
        out = merger.merge(str(tmp_path / "out.pdf"), per_page=2)
        assert out["page_count"] == 1


# ---------------------------------------------------------------------------
# #4 per_page 校验
# ---------------------------------------------------------------------------

class TestPerPageValidation:
    def _merger(self, tmp_path):
        pdf = _write_pdf(str(tmp_path / "inv.pdf"), n_pages=2)
        merger = InvoiceMerger()
        merger.analyze([pdf])
        return merger

    @pytest.mark.parametrize("bad", [0, -1, 3, 5, 7, 8, 999])
    def test_invalid_per_page_rejected(self, tmp_path, bad):
        merger = self._merger(tmp_path)
        with pytest.raises(ValueError) as ei:
            merger.merge(str(tmp_path / "out.pdf"), per_page=bad)
        assert "仅支持" in str(ei.value)

    @pytest.mark.parametrize("good", [1, 2, 4, 6, 9])
    def test_valid_per_page_ok(self, tmp_path, good):
        merger = self._merger(tmp_path)
        out = merger.merge(str(tmp_path / f"out_{good}.pdf"), per_page=good)
        assert out["invoices_count"] == 2

    def test_per_page_negative_no_fake_success(self, tmp_path):
        """per_page=-1 旧版返回假成功（产物从未生成，下载 404）。"""
        merger = self._merger(tmp_path)
        with pytest.raises(ValueError):
            merger.merge(str(tmp_path / "out.pdf"), per_page=-1)
        assert not os.path.exists(str(tmp_path / "out.pdf"))


# ---------------------------------------------------------------------------
# #5 台账导出公式注入
# ---------------------------------------------------------------------------

class TestFormulaInjection:
    def _row(self, **kw):
        defaults = dict(
            source_file="a.pdf",
            buyer_name="北京创信卓远信息技术有限公司",
            remark="正常备注",
        )
        defaults.update(kw)
        return ExportRowRequest(**defaults)

    @pytest.mark.parametrize("payload", ["=cmd|' /C calc'!A1", "+SUM(A1)", "-2+3", "@WEBSERVICE(x)"])
    def test_csv_neutralized(self, payload):
        data = export_csv([self._row(buyer_name=payload)]).decode("utf-8-sig")
        # 单元格被前置单引号强制按文本处理，不再以公式前缀开头
        assert "'" + payload in data

    def test_xlsx_neutralized(self):
        row = self._row(buyer_name="=HYPERLINK(\"http://evil.example\",\"点我\")")
        buf = io.BytesIO(export_xlsx([row]))
        from openpyxl import load_workbook

        wb = load_workbook(buf)
        ws = wb.active
        # 购买方名称 = 第 5 列，第 2 行
        cell_value = ws.cell(row=2, column=5).value
        assert cell_value.startswith("'")
        assert not cell_value.startswith("=")

    def test_normal_text_untouched(self):
        assert _sanitize_cell("北京创信卓远") == "北京创信卓远"
        assert _sanitize_cell(None) is None
        assert _sanitize_cell("单价-重量法") == "单价-重量法"  # 前缀在中间不受影响
        assert _sanitize_cell("-开头").startswith("'-")


# ---------------------------------------------------------------------------
# #6 台账提取跨页串扰
# ---------------------------------------------------------------------------

def _invoice_page(page, number):
    """按官方横版版式合成一页数电票文字层（坐标与 test_invoice_extract 夹具一致）。"""
    def put(x, y, s):
        font = "helv" if s.isascii() else "china-s"
        page.insert_text((x, y), s, fontsize=9, fontname=font)

    put(161.7, 40, "电子发票（增值税专用发票）")
    put(438.0, 39.4, "发票号码：")
    put(484.0, 39.4, number)
    put(438.0, 56.6, "开票日期：")
    put(484.0, 56.6, "2026年08月25日")
    put(32.7, 103.5, "名称：")
    put(57.0, 103.5, "中国人民财产保险股份有限公司")
    put(317.6, 103.5, "名称：")
    put(341.0, 103.5, "北京创信卓远信息技术有限公司")
    put(153.1, 132.6, "91100000710931483R")
    put(437.9, 132.6, "9111010859062383XH")
    put(13.8, 167.9, "*软件服务*技术服务费")
    put(394.6, 268.5, "695086.79")
    put(546.5, 268.5, "41705.21")
    put(406.8, 287.7, "（小写）")
    put(447.4, 287.7, "¥736792.00")
    put(34.0, 318.0, "合同编号HT2026-0825")


class TestCrossPageExtraction:
    def test_two_invoices_rejected_not_merged(self):
        """两页不同号码发票：旧版产出 1 条串页记录；新版必须整体拒绝。"""
        doc = fitz.open()
        _invoice_page(doc.new_page(width=595.28, height=396.85), "26112000003559581871")
        _invoice_page(doc.new_page(width=595.28, height=396.85), "26112000003559599999")
        data = doc.tobytes()
        doc.close()
        with pytest.raises(InvoiceExtractError) as ei:
            extract_pdf_bytes(data, "two.pdf")
        assert "多张发票" in str(ei.value) or "拆分" in str(ei.value)

    def test_invoice_plus_attachment_page_warns(self):
        """发票页 + 附件文字页：取发票页记录，附加人工核对提示，不混页。"""
        doc = fitz.open()
        _invoice_page(doc.new_page(width=595.28, height=396.85), "26112000003559581871")
        att = doc.new_page(width=595.28, height=396.85)
        att.insert_text((72, 72), "附件：购销合同补充说明，本页为非发票内容页，仅作说明用途。",
                        fontsize=10, fontname="china-s")
        data = doc.tobytes()
        doc.close()
        rec = extract_pdf_bytes(data, "mix.pdf")
        assert rec.invoice_number == "26112000003559581871"
        assert any("仅第 1 页" in w for w in rec.warnings)

    def test_single_page_invoice_unchanged(self):
        doc = fitz.open()
        _invoice_page(doc.new_page(width=595.28, height=396.85), "26112000003559581871")
        data = doc.tobytes()
        doc.close()
        rec = extract_pdf_bytes(data, "one.pdf")
        assert rec.invoice_number == "26112000003559581871"
        assert (rec.amount_without_tax, rec.tax_amount, rec.total_with_tax) == (
            "695086.79", "41705.21", "736792.00",
        )
        assert rec.warnings == []


# ---------------------------------------------------------------------------
# #7 PDF 分割 ranges 上限
# ---------------------------------------------------------------------------

class TestSplitRangesGuard:
    def _splitter(self, tmp_path):
        pdf = _write_pdf(str(tmp_path / "doc.pdf"), n_pages=3)
        return PDFSplitter(pdf)

    def test_too_many_parts_rejected(self, tmp_path):
        splitter = self._splitter(tmp_path)
        value = ",".join(["1-1"] * 201)
        with pytest.raises(ValueError) as ei:
            splitter.split(mode="ranges", value=value, output_dir=str(tmp_path))
        assert "过多" in str(ei.value)

    def test_overlong_value_rejected(self, tmp_path):
        splitter = self._splitter(tmp_path)
        value = "1," * 3000  # 6000 字符
        with pytest.raises(ValueError) as ei:
            splitter.split(mode="ranges", value=value, output_dir=str(tmp_path))
        assert "过长" in str(ei.value)

    def test_garbage_part_chinese_error(self, tmp_path):
        splitter = self._splitter(tmp_path)
        with pytest.raises(ValueError) as ei:
            splitter.split(mode="ranges", value="abc", output_dir=str(tmp_path))
        assert "格式无效" in str(ei.value)

    def test_normal_ranges_still_work(self, tmp_path):
        splitter = self._splitter(tmp_path)
        out = splitter.split(mode="ranges", value="1-2,3", output_dir=str(tmp_path))
        assert len(out) == 2


# ---------------------------------------------------------------------------
# #14 PDF 转 Word 页码 1 基
# ---------------------------------------------------------------------------

class TestPdfToWordPagesOneBased:
    def test_single_page_index(self):
        assert PDFToWordConverter._parse_pages("1", 3) == [0]
        assert PDFToWordConverter._parse_pages("2", 3) == [1]
        assert PDFToWordConverter._parse_pages("3", 3) == [2]

    def test_range_index(self):
        assert PDFToWordConverter._parse_pages("1-3", 3) == [0, 1, 2]
        assert PDFToWordConverter._parse_pages("2-3", 3) == [1, 2]

    def test_legacy_zero_based_rejected(self):
        """旧 0 基语义（"0" 指第 1 页）必须失效：0 不再是合法页码。"""
        with pytest.raises(ValueError):
            PDFToWordConverter._parse_pages("0", 3)

    def test_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            PDFToWordConverter._parse_pages("4", 3)

    def test_huge_range_clamped(self):
        assert PDFToWordConverter._parse_pages("1-99999999", 3) == [0, 1, 2]
