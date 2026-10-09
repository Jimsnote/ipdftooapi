"""P0 事故修复回归测试（2026-10-08 香港机 OOM + API 挂死事故）。

锁定三个修复的行为，防止回归：
- 修复① 死锁防护（ofd_validator）：_destroy_mp_pool 锁内零阻塞、worker
  必被收割不留僵尸；池获取/apply 有超时与降级路径；
- 修复② 方案 A 字体补丁（ofd_converter）：系统 CJK 字体占名「宋体」，
  幂等可重入；
- 修复③ invoice_merger 流式写盘 + 异常路径关闭文档：输出正确性与
  旧实现一致，多页合并页数/尺寸不受流式化影响。
"""
import os
import time

import fitz
import pytest
from PIL import Image as PILImage

from app.services import ofd_converter as ofd_converter_mod
from app.services import ofd_validator as ov
from app.services.invoice_merger import InvoiceMerger


def _write_pdf(path, n_pages=1, width=595.28, height=842):
    doc = fitz.open()
    for _ in range(n_pages):
        page = doc.new_page(width=width, height=height)
        page.insert_text((72, 100), "invoice", fontsize=12)
    doc.save(path)
    return path


# ---------------------------------------------------------------------------
# 修复① 进程池死锁防护
# ---------------------------------------------------------------------------

class TestPoolDestroyDefensive:
    def test_destroy_clears_reference_fast(self):
        """destroy 后模块引用立即置空，且销毁过程不长时间阻塞。"""
        pool = ov._get_mp_pool()
        assert ov._mp_pool is pool
        t0 = time.monotonic()
        ov._destroy_mp_pool()
        elapsed = time.monotonic() - t0
        assert ov._mp_pool is None
        # 正常 terminate+join 秒级完成；卡死场景由 SIGKILL 兜底限时 5s+2s
        assert elapsed < 10, f"destroy 耗时 {elapsed:.1f}s，疑似回归锁内阻塞"

    def test_destroy_reaps_workers_no_zombie_alive(self):
        """destroy 后池内 worker 必须全部退出（is_alive False），不留活尸。"""
        pool = ov._get_mp_pool()
        workers = list(getattr(pool, "_pool", None) or [])
        ov._destroy_mp_pool()
        for w in workers:
            assert not w.is_alive(), "destroy 后仍有 worker 存活，僵尸收割回归"

    def test_get_pool_after_destroy_creates_new_pool(self):
        """销毁后再次获取应得到全新健康池（惰性重建语义）。"""
        old = ov._get_mp_pool()
        ov._destroy_mp_pool()
        fresh = ov._get_mp_pool()
        assert fresh is not old
        assert ov._mp_pool is fresh
        # 清理，避免测试进程残留池 worker
        ov._destroy_mp_pool()

    def test_destroy_with_no_pool_noop(self):
        """未初始化时 destroy 是安全 no-op。"""
        ov._destroy_mp_pool()
        assert ov._mp_pool is None


# ---------------------------------------------------------------------------
# 修复② 方案 A 字体补丁
# ---------------------------------------------------------------------------

class TestEasyofdFontPatch:
    def test_patch_registers_songti_with_candidate_font(self, tmp_path, monkeypatch):
        """给定通过双覆盖自检的字体文件时，「宋体」应被注册进 reportlab。"""
        import shutil

        # 找一个本机真实 TTF 复制为候选（避开对系统字体分布的硬依赖）；
        # 自检用 monkeypatch 强制通过（本机字体不保证 ASCII+CJK 双覆盖）
        sources = [
            r"C:\Windows\Fonts\arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
        src = next((s for s in sources if os.path.exists(s)), None)
        if not src:
            pytest.skip("本环境无可用 TTF 字体文件")
        ttf = tmp_path / "fake-cjk.ttf"
        shutil.copyfile(src, str(ttf))
        monkeypatch.setattr(ofd_converter_mod, "_CJK_FONT_CANDIDATES", [str(ttf)])
        monkeypatch.setattr(ofd_converter_mod, "_font_covers_ascii_cjk", lambda p: True)

        from reportlab.pdfbase import pdfmetrics

        # 补丁幂等：先强制走一次（若此前已注册过「宋体」则先卸不可行，
        # 直接验证 getFont 可用且不抛错）
        ofd_converter_mod._patch_easyofd_font()
        font_obj = pdfmetrics.getFont("宋体")  # 不抛 KeyError 即注册成功
        assert font_obj is not None

    def test_font_covers_ascii_cjk_rejects_latin_only(self, tmp_path):
        """自检应拒绝仅有 ASCII 无 CJK 的字体（如 DejaVu/Arial）。"""
        import shutil

        sources = [
            r"C:\Windows\Fonts\arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        ]
        src = next((s for s in sources if os.path.exists(s)), None)
        if not src:
            pytest.skip("本环境无可用 TTF 字体文件")
        ttf = tmp_path / "latin-only.ttf"
        shutil.copyfile(src, str(ttf))
        assert ofd_converter_mod._font_covers_ascii_cjk(str(ttf)) is False

    def test_droid_sans_fallback_rejected_by_self_check(self):
        """回归（2026-10-09 航空行程单）：DroidSansFallback 仅含 CJK 无 ASCII，
        必须被自检拒绝，否则西文/数字整片画空。服务器路径不存在时跳过。"""
        for p in (
            "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
            "/usr/share/fonts/truetype/droid/DroidSansFallback.ttf",
        ):
            if os.path.exists(p):
                assert ofd_converter_mod._font_covers_ascii_cjk(p) is False
                return
        pytest.skip("本环境无 DroidSansFallback 字体")

    def test_patch_idempotent_no_raise(self):
        """重复调用补丁不抛错（进程内幂等）。"""
        ofd_converter_mod._patch_easyofd_font()
        ofd_converter_mod._patch_easyofd_font()



# ---------------------------------------------------------------------------
# 修复③ invoice_merger 流式写盘 + finally 关闭文档
# ---------------------------------------------------------------------------

class TestInvoiceMergerStreaming:
    def test_multi_invoice_merge_page_count_correct(self, tmp_path):
        """流式化后输出正确性不变：2 页 × 5 票、每页 2 张 → 5 页输出。"""
        paths = [
            _write_pdf(str(tmp_path / f"inv{i}.pdf"), n_pages=2) for i in range(5)
        ]
        merger = InvoiceMerger()
        merger.analyze(paths)
        out = merger.merge(
            str(tmp_path / "out.pdf"), per_page=2, page_numbers=True
        )
        assert out["page_count"] == 5
        assert out["invoices_count"] == 10
        doc = fitz.open(str(tmp_path / "out.pdf"))
        try:
            assert doc.page_count == 5
            # 页面尺寸应为 A4 竖版（流式不改变布局语义）
            page = doc[0]
            assert abs(page.rect.width - 595.28) < 30
            assert abs(page.rect.height - 841.89) < 30
        finally:
            doc.close()

    def test_merge_output_renders_content(self, tmp_path):
        """流式写盘产物可正常渲染（页面非空白）。"""
        pdf = _write_pdf(str(tmp_path / "inv.pdf"), n_pages=1)
        merger = InvoiceMerger()
        merger.analyze([pdf])
        out_path = str(tmp_path / "out.pdf")
        merger.merge(out_path, per_page=1, page_numbers=False)
        doc = fitz.open(out_path)
        try:
            pix = doc[0].get_pixmap(matrix=fitz.Matrix(0.2, 0.2), alpha=False)
            samples = pix.samples
            # 非全白：至少存在非 255 的像素（发票页有文字）
            assert any(b != 255 for b in samples[:4000]) or len(set(samples[:4000])) > 1
        finally:
            doc.close()

    def test_paste_sheet_layout_still_works(self, tmp_path):
        """凭证粘贴模式（横版 + 装订线）在流式化后不受影响。"""
        pdf = _write_pdf(str(tmp_path / "inv.pdf"), n_pages=4)
        merger = InvoiceMerger()
        merger.analyze([pdf])
        out = merger.merge(
            str(tmp_path / "out.pdf"),
            per_page=2,
            layout="paste_sheet",
            binding_mm=30,
        )
        assert out["page_count"] == 2
        doc = fitz.open(str(tmp_path / "out.pdf"))
        try:
            assert doc[0].rect.width > doc[0].rect.height  # 横版
        finally:
            doc.close()
