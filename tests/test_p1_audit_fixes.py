"""P1 审计修复回归测试（2026-09-29 全项目挖虫报告 #8/#9/#10/#12/#13/#15/#16/#19/#26/#27）。

逐项锁定"修复前实测可复现"的场景，防止回归：
- #8  证件照行内 src 绕过图片总量校验（200×20MB ≈ 4GB 合法请求体）
- #9  水印图片先全量读内存、后做 10MB 检查
- #10 OCR 结果解析不兼容 PaddleOCR 3.x（ndarray 真值判断抛 ValueError）
- #12 OCR precheck 加密 PDF 裸 500
- #13 加密 PDF 喂 5 个 pypdf 系服务全部 500（应 400 中文引导）
- #15 台账导出行内 \\x00 → openpyxl IllegalCharacterError 裸 500
- #16 旧端点 /ofd-invoice/merge 重复调用混入上次产物
- #19 压缩 fallback 越压越大仍报成功
- #26 证件照水印透明度方向与前端预览相反
- #27 证件照渲染参数校验族（10⁹mm 坐标 OverflowError / NaN 旋转静默忽略 / 缺 src 静默跳过）
"""
import asyncio
import io
import os

import fitz
import numpy as np
import pytest
from PIL import Image as PILImage
from pydantic import ValidationError

from app.core.file_security import make_task_dir
from app.models.invoice_extract import ExportRowRequest
from app.routers import ofd_invoice
from app.routers.pdf import raise_processing_error
from app.schemas import id_photo as id_photo_schema
from app.schemas.id_photo import CanvasImage, IDPhotoRenderRequest, TiledWatermark
from app.services.id_photo_renderer import IDPhotoRenderer
from app.services.invoice_extract import export_csv, export_xlsx
from app.services.ocr import engine as ocr_engine
from app.services.ocr.precheck import precheck
from app.services.ocr.scan_to_pdf import build_searchable_pdf
from app.services.pdf_compressor import PDFCompressor
from app.services.pdf_splitter import PDFSplitter


# ---------------------------------------------------------------------------
# 工具：合成 PDF / PNG / 加密 PDF
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
    doc.save(path, encryption=fitz.PDF_ENCRYPT_AES_256, user_pw=password, owner_pw=password)
    doc.close()
    return path


def _png_bytes(w=50, h=50):
    img = PILImage.new("RGB", (w, h), "white")
    img.putpixel((10, 10), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# #8 证件照：行内 src 计入图片总量校验
# ---------------------------------------------------------------------------

class TestInlineSrcPayloadLimit:
    def test_inline_src_counts_toward_total(self, monkeypatch):
        """3 项 × 400 字符行内 src，总量 1200 超过限额 1000 → 拒绝。
        修复前行内 src 完全不计入，此请求可通过。"""
        monkeypatch.setattr(id_photo_schema, "MAX_TOTAL_IMAGE_B64_CHARS", 1000)
        imgs = [
            CanvasImage(id=f"i{k}", x=0, y=0, width=10, height=10, src="A" * 400)
            for k in range(3)
        ]
        with pytest.raises(ValidationError):
            IDPhotoRenderRequest(images=imgs)

    def test_source_images_still_counted(self, monkeypatch):
        """基线不回退：source_images 总量超限仍然拒绝。"""
        monkeypatch.setattr(id_photo_schema, "MAX_TOTAL_IMAGE_B64_CHARS", 1000)
        with pytest.raises(ValidationError):
            IDPhotoRenderRequest(source_images={"a": "A" * 1200})

    def test_within_limit_passes(self, monkeypatch):
        monkeypatch.setattr(id_photo_schema, "MAX_TOTAL_IMAGE_B64_CHARS", 1000)
        imgs = [
            CanvasImage(id=f"i{k}", x=0, y=0, width=10, height=10, src="A" * 400)
            for k in range(2)
        ]
        req = IDPhotoRenderRequest(images=imgs)
        assert len(req.images) == 2


# ---------------------------------------------------------------------------
# #9 水印图片：声明大小预检 + 流式落盘
# ---------------------------------------------------------------------------

class TestWatermarkImageSizeGuard:
    def _prepare_task(self):
        task_id, task_dir = make_task_dir(ofd_invoice.TEMP_DIR)
        _write_pdf(os.path.join(task_dir, "input.pdf"))
        return task_id, task_dir

    def test_oversized_declared_size_rejected_before_read(self):
        """声明 11MB（内容其实为空）→ 直接 400，不再先整读进内存。"""
        from fastapi import HTTPException
        from starlette.datastructures import UploadFile

        from app.routers.pdf import watermark_pdf

        task_id, task_dir = self._prepare_task()
        # 内容为空：若实现仍是 await read() 后判 len，会走到 400；若先判 size 也是 400。
        # 关键差异在 11MB 声明 + 空 body——旧实现读空内容反而通过 size 检查？
        # 不——旧实现 len(image_bytes)=0 < 10MB 会"通过"并继续。新实现按声明 size 拦截。
        up = UploadFile(file=io.BytesIO(b""), filename="wm.png", size=11 * 1024 * 1024)
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(
                watermark_pdf(
                    task_id=task_id, wm_type="image", text="", layout="tile",
                    opacity=0.2, color="gray", fontsize=48.0, width_fraction=0.3,
                    pages="", wm_image=up,
                )
            )
        assert exc_info.value.status_code == 400
        assert "10MB" in exc_info.value.detail

    def test_normal_small_image_watermark_ok(self):
        """正常小图走流式落盘路径，端到端成功。"""
        from starlette.datastructures import UploadFile

        from app.routers.pdf import watermark_pdf

        task_id, task_dir = self._prepare_task()
        data = _png_bytes()
        up = UploadFile(file=io.BytesIO(data), filename="wm.png", size=len(data))
        resp = asyncio.run(
            watermark_pdf(
                task_id=task_id, wm_type="image", text="", layout="tile",
                opacity=0.2, color="gray", fontsize=48.0, width_fraction=0.3,
                pages="", wm_image=up,
            )
        )
        assert resp.status == "completed"


# ---------------------------------------------------------------------------
# #10 OCR：ndarray 归一化
# ---------------------------------------------------------------------------

class TestOcrPolysNormalization:
    def _fake_ocr(self, rec_texts, dt_polys):
        class _FakeRes:
            def __init__(self, j):
                self.json = j

        class _FakeOcr:
            def predict(self, path):
                return [_FakeRes({"rec_texts": rec_texts, "dt_polys": dt_polys})]

        return _FakeOcr()

    def test_ndarray_polys_and_texts_no_ambiguous_error(self, monkeypatch, tmp_path):
        """PaddleOCR 3.x 形态：dt_polys 为 ndarray → 修复前 `if polys` 抛
        ValueError: truth value of an array is ambiguous。"""
        polys = np.array([[[1, 1], [10, 1], [10, 10], [1, 10]]], dtype=np.float32)
        texts = np.array(["测试文本"])  # ndarray 形态的 rec_texts
        monkeypatch.setattr(ocr_engine, "get_ocr", lambda: self._fake_ocr(texts, polys))

        img_path = str(tmp_path / "p.png")
        PILImage.new("RGB", (32, 32), "white").save(img_path)

        out = ocr_engine.ocr_image(img_path)
        assert len(out) == 1
        bbox, text = out[0]
        assert text == "测试文本"
        assert not isinstance(bbox, np.ndarray)  # 已归一化为 list
        assert bbox[0] == [1.0, 1.0]

    def test_list_form_still_works(self, monkeypatch, tmp_path):
        """PaddleOCR 2.x list 形态不回退。"""
        monkeypatch.setattr(
            ocr_engine,
            "get_ocr",
            lambda: self._fake_ocr(["hello"], [[[0, 0], [5, 0], [5, 5], [0, 5]]]),
        )
        img_path = str(tmp_path / "p.png")
        PILImage.new("RGB", (32, 32), "white").save(img_path)
        out = ocr_engine.ocr_image(img_path)
        # ocr_image 返回 [(bbox, text), ...]
        assert out == [([[0, 0], [5, 0], [5, 5], [0, 5]], "hello")]

    def test_build_searchable_pdf_with_ndarray_bbox(self, tmp_path):
        """scan_to_pdf 文字层：bbox 为 ndarray 时 `if bbox` 同病，应正常建 PDF。"""
        img_path = str(tmp_path / "page.png")
        PILImage.new("RGB", (30, 20), "white").save(img_path)
        results = [[(np.array([[1.0, 1.0], [10, 1], [10, 8], [1, 8]]), "scan text")]]
        out_path = str(tmp_path / "out.pdf")
        build_searchable_pdf([img_path], results, out_path)
        assert os.path.exists(out_path)


# ---------------------------------------------------------------------------
# #12 OCR precheck：加密/损坏 PDF 不再裸异常
# ---------------------------------------------------------------------------

class TestOcrPrecheckEncrypted:
    def test_encrypted_pdf_rejected_with_chinese_message(self, tmp_path):
        """修复前：precheck 内 p.get_text() 对加密 PDF 抛异常直接逃逸 → 裸 500。"""
        path = _write_encrypted_pdf(str(tmp_path / "enc.pdf"))
        ok, msg, has_text = precheck(path, is_image=False)
        assert ok is False
        assert "已加密" in msg
        assert has_text is False

    def test_corrupt_pdf_rejected_cleanly(self, tmp_path):
        path = str(tmp_path / "bad.pdf")
        with open(path, "wb") as f:
            f.write(b"not a pdf at all")
        ok, msg, _ = precheck(path, is_image=False)
        assert ok is False
        assert "无法解析" in msg

    def test_normal_pdf_passes(self, tmp_path):
        path = str(tmp_path / "ok.pdf")
        doc = fitz.open()
        page = doc.new_page(width=595.28, height=842)
        page.insert_text((72, 72), "plain text content " * 10)
        doc.save(path)
        doc.close()
        ok, _, has_text = precheck(path, is_image=False)
        assert ok is True


# ---------------------------------------------------------------------------
# #13 加密 PDF：pypdf 系服务 400 中文引导
# ---------------------------------------------------------------------------

class TestEncryptedPdf400Mapping:
    def test_file_not_decrypted_error_maps_400(self):
        from pypdf.errors import FileNotDecryptedError

        with pytest.raises(Exception) as exc_info:
            raise_processing_error(FileNotDecryptedError("File has not been decrypted"))
        assert getattr(exc_info.value, "status_code", None) == 400
        assert "解密" in exc_info.value.detail

    def test_require_password_message_maps_400(self):
        """to-word 的 pdf2docx ConversionException('Require password...') 同病。"""
        with pytest.raises(Exception) as exc_info:
            raise_processing_error(RuntimeError("Require password: doc is encrypted"))
        assert getattr(exc_info.value, "status_code", None) == 400
        assert "解密" in exc_info.value.detail

    def test_splitter_on_encrypted_pdf_maps_400_end_to_end(self, tmp_path):
        """实测链路：加密 PDF → PDFSplitter 抛 FileNotDecryptedError → 400。"""
        from pypdf.errors import FileNotDecryptedError

        path = _write_encrypted_pdf(str(tmp_path / "enc.pdf"))
        with pytest.raises(FileNotDecryptedError):
            PDFSplitter(path).split(output_dir=str(tmp_path))
        with pytest.raises(Exception) as exc_info:
            raise_processing_error(FileNotDecryptedError("File has not been decrypted"))
        assert getattr(exc_info.value, "status_code", None) == 400

    def test_unrelated_error_still_500(self):
        with pytest.raises(Exception) as exc_info:
            raise_processing_error(RuntimeError("disk exploded"))
        assert getattr(exc_info.value, "status_code", None) == 500


# ---------------------------------------------------------------------------
# #15 台账导出：控制字符剥离
# ---------------------------------------------------------------------------

class TestControlCharExport:
    def test_xlsx_with_nul_bytes_no_crash(self):
        """修复前：remark 含 \\x00 → openpyxl IllegalCharacterError 裸 500。"""
        row = ExportRowRequest(source_file="a.pdf", remark="bad\x00char\x0b!")
        data = export_xlsx([row])  # 不抛异常
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(data))
        # remark 是第 13 列（COLUMNS 顺序）
        assert wb.active.cell(row=2, column=13).value == "badchar!"

    def test_control_chars_stripped_before_formula_check(self):
        """\\x00=cmd 剥离控制字符后仍命中公式前缀 → 前置单引号。"""
        row = ExportRowRequest(source_file="b.pdf", invoice_number="\x00=1+1")
        csv_bytes = export_csv([row])
        assert b"\x00" not in csv_bytes
        assert b"'=1+1" in csv_bytes

    def test_normal_text_untouched(self):
        row = ExportRowRequest(source_file="c.pdf", remark="正常备注文本")
        data = export_xlsx([row])
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(data))
        assert wb.active.cell(row=2, column=13).value == "正常备注文本"


# ---------------------------------------------------------------------------
# #16 旧端点 merge：不混入上次产物
# ---------------------------------------------------------------------------

class TestOfdInvoiceMergePrefixFilter:
    def test_repeat_merge_excludes_previous_output(self):
        """同 task_id 二次 merge：目录里的 merged_invoices.pdf 不再被当发票重拼。
        修复前收集条件只有 .pdf 后缀，"已合并 3 张"实际 2 张。"""
        task_id, task_dir = make_task_dir(ofd_invoice.TEMP_DIR)
        _write_pdf(os.path.join(task_dir, "invoice_001.pdf"))
        # 上次 merge 的遗留产物（1 页有效 PDF）
        _write_pdf(os.path.join(task_dir, "merged_invoices.pdf"))

        resp = ofd_invoice.merge_ofd_invoices(
            task_id=task_id,
            per_page=1,
            margin="standard",
            crop_marks=True,
            page_numbers=True,
            layout="stacked",
            binding_mm=0,
        )
        assert resp.status == "completed"
        assert "已合并 1 张" in resp.message  # 不是 2 张


# ---------------------------------------------------------------------------
# #19 压缩 fallback：越压越大时回传原文件
# ---------------------------------------------------------------------------

class TestFallbackCompressNoGrow:
    def test_output_never_larger_than_input(self, tmp_path, monkeypatch):
        """pypdf fallback 对已优化的小文件常越压越大；修复后保证产物 ≤ 输入。"""
        src = _write_pdf(str(tmp_path / "in.pdf"))  # 极小 PDF，fallback 大概率变大
        in_size = os.path.getsize(src)
        out_path = str(tmp_path / "out.pdf")

        compressor = PDFCompressor(src)
        monkeypatch.setattr(compressor, "_find_gs", lambda: None)  # 强制走 fallback
        compressor.compress(out_path, level="normal")

        assert os.path.exists(out_path)
        assert os.path.getsize(out_path) <= in_size


# ---------------------------------------------------------------------------
# #19 关联（部署冒烟新发现）：加密 PDF 在有 gs 的环境产出空白壳
# ---------------------------------------------------------------------------

class TestCompressorEncryptedPrecheck:
    def test_encrypted_pdf_rejected_at_entry(self, tmp_path):
        """生产 gs 10.x 对加密 PDF returncode=0 且产出空白壳（静默废文件）；
        pypdf fallback 抛 FileNotDecryptedError。入口统一拦截 → ValueError→400。"""
        from pypdf import PdfReader  # noqa: F401 确认依赖在场

        enc = _write_encrypted_pdf(str(tmp_path / "enc.pdf"))
        out = str(tmp_path / "out.pdf")
        with pytest.raises(ValueError) as exc_info:
            PDFCompressor(enc).compress(out, level="normal")
        assert "已加密" in str(exc_info.value)
        assert not os.path.exists(out)  # 不产出任何废文件

    def test_normal_pdf_still_compressible(self, tmp_path, monkeypatch):
        src = _write_pdf(str(tmp_path / "in.pdf"))
        out = str(tmp_path / "out.pdf")
        compressor = PDFCompressor(src)
        monkeypatch.setattr(compressor, "_find_gs", lambda: None)
        compressor.compress(out, level="normal")
        assert os.path.exists(out)


# ---------------------------------------------------------------------------
# #26 证件照水印：透明度方向与前端预览一致
# ---------------------------------------------------------------------------

class TestWatermarkAlphaDirection:
    def _min_luma(self, opacity):
        renderer = IDPhotoRenderer()
        wm = TiledWatermark(text="W", font_size=10, color="#000000", opacity=opacity, enabled=True)
        renderer._render_tiled_watermark(wm)
        arr = np.asarray(renderer.canvas.convert("L"))
        return int(arr.min())

    def test_high_opacity_is_dark(self):
        """α=0.8 黑字 → 与白底合成 ≈ 51 灰度（旧实现向白预混 ≈ 204，几乎看不见）。"""
        assert self._min_luma(0.8) <= 60

    def test_low_opacity_is_light(self):
        """α=0.15 黑字 → ≈ 217 灰度（很淡）；旧实现反向 ≈ 38（很实）。"""
        lo = self._min_luma(0.15)
        assert 190 <= lo <= 235

    def test_full_opacity_is_pure_color(self):
        assert self._min_luma(1.0) <= 10


# ---------------------------------------------------------------------------
# #27 证件照渲染参数校验族
# ---------------------------------------------------------------------------

class TestIDPhotoParamValidation:
    def test_huge_coordinate_rejected(self):
        """修复前：10⁹mm 坐标进 PIL paste 超 C int → OverflowError 裸 500。"""
        with pytest.raises(ValidationError):
            CanvasImage(id="a", x=1e9, y=0, width=10, height=10)

    def test_nan_rotation_rejected(self):
        """修复前：NaN rotation 被放行且 `NaN % 360 > 0.1` 为 False → 旋转静默忽略。"""
        with pytest.raises(ValidationError):
            CanvasImage(id="a", x=0, y=0, width=10, height=10, rotation=float("nan"))

    def test_inf_coordinate_rejected(self):
        with pytest.raises(ValidationError):
            CanvasImage(id="a", x=float("inf"), y=0, width=10, height=10)

    def test_crop_x_out_of_range_rejected(self):
        """注释约定 [-0.5, 0.5]，修复前 crop_x=99 静默产出废片。"""
        with pytest.raises(ValidationError):
            CanvasImage(id="a", x=0, y=0, width=10, height=10, crop_x=99)

    def test_crop_zoom_below_1_rejected(self):
        with pytest.raises(ValidationError):
            CanvasImage(id="a", x=0, y=0, width=10, height=10, crop_zoom=0.5)

    def test_crop_zoom_over_slider_max_rejected(self):
        """前端滑杆 max=3，API 侧留 4 余量；99 明显异常。"""
        with pytest.raises(ValidationError):
            CanvasImage(id="a", x=0, y=0, width=10, height=10, crop_zoom=99)

    def test_valid_params_pass(self):
        img = CanvasImage(id="a", x=10, y=10, width=30, height=40, rotation=45, crop_zoom=1.5, crop_x=-0.3)
        assert img.rotation == 45

    def test_missing_src_ref_raises_instead_of_silent_skip(self, tmp_path):
        """修复前：src_ref 指向不存在的键 → 静默跳过该图片，用户拿到缺图成品无感知。"""
        out_path = str(tmp_path / "layout.pdf")
        renderer = IDPhotoRenderer()
        img = CanvasImage(id="i1", x=0, y=0, width=30, height=40, src_ref="missing_key")
        with pytest.raises(ValueError) as exc_info:
            renderer.render([img], [], TiledWatermark(), out_path, source_images={})
        assert "缺少图片数据" in str(exc_info.value)


# ---------------------------------------------------------------------------
# R1 字面量 NaN/Infinity：422 处理器不再自崩成裸 500
# ---------------------------------------------------------------------------

class TestRequestValidationErrorJson:
    def _client(self):
        from fastapi.testclient import TestClient

        from app.main import app

        return TestClient(app)

    def test_nan_literal_returns_422_json(self):
        """修复前：rotation: NaN 被 schema 正确拒绝，但错误详情携带 input=nan，
        框架默认 422 处理器序列化崩溃 → 客户端收到裸 500 纯文本。"""
        r = self._client().post(
            "/api/v1/id-photo/render",
            json={
                "orientation": "portrait",
                "images": [
                    {"id": "a", "x": 0, "y": 0, "width": 10, "height": 10, "rotation": float("nan")}
                ],
            },
        )
        assert r.status_code == 422
        data = r.json()  # 能解析即证明响应是合法 JSON
        assert isinstance(data["detail"], list)

    def test_infinity_literal_returns_422_json(self):
        r = self._client().post(
            "/api/v1/id-photo/render",
            json={
                "orientation": "portrait",
                "images": [
                    {"id": "a", "x": float("inf"), "y": 0, "width": 10, "height": 10}
                ],
            },
        )
        assert r.status_code == 422
        assert isinstance(r.json()["detail"], list)

    def test_normal_validation_error_unaffected(self):
        """普通 422 路径不回退：缺必填字段仍返回标准结构。"""
        r = self._client().post("/api/v1/id-photo/render", json={"orientation": "bogus"})
        assert r.status_code == 422
        assert isinstance(r.json()["detail"], list)
