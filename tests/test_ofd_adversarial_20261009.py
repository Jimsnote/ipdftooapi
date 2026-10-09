"""2026-10-09 OFD 对抗式审查（第一轮）修复的回归测试。

覆盖三个修复项：
- P1-1：ofd_validator 并发信号量 workers 2 语义收紧（断言常量，防止未来
  无意改回引发内存预算失控）
- P1-2：easyofd cmp_offset 补丁的 g 分支越界保护（损坏 DeltaRule 不再
  IndexError 炸掉整个 to_pdf）
- P1-6：发票合并 OFD 转换升级进程池（convert_ofd_batch_async：预扫描拦截、
  转换失败 fail-fast + 中间产物清理、正常路径产物落盘）

本地 venv 无 pytest-asyncio，async 路径用 asyncio.run 同步包装。
"""

import asyncio
import os
import zipfile

import pytest
from fastapi import HTTPException

from app.services import invoice_merge_shared
from app.services import ofd_validator
from app.services.ofd_converter import _patch_easyofd_cmp_offset


def _run(coro):
    """同步测试跑 async 协程（本地无 pytest-asyncio）。"""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# P1-1 并发常量语义
# ---------------------------------------------------------------------------

class TestConcurrencyBudget:
    def test_semaphore_is_one_per_worker(self):
        """workers 1→2 后每 worker 并发收紧为 1（全局 = 1 × workers = 2）。"""
        assert ofd_validator._CONCURRENCY == 1

    def test_pool_worker_count_matches_concurrency(self):
        """进程池 worker 数与信号量一致（池自身天然限并发）。"""
        import inspect

        src = inspect.getsource(ofd_validator._get_mp_pool)
        assert "processes=_CONCURRENCY" in src


# ---------------------------------------------------------------------------
# P1-2 cmp_offset g 分支越界保护
# ---------------------------------------------------------------------------

class TestCmpOffsetGBranchGuard:
    @classmethod
    def setup_class(cls):
        # 确保 patch 已应用（模块 import 时自动应用，此处防御性重复）
        _patch_easyofd_cmp_offset()
        from easyofd.draw.draw_pdf import DrawPDF

        cls.cmp_offset = DrawPDF.cmp_offset

    def _run(self, delta_rule, text="AB"):
        # DrawPDF.cmp_offset 经 cls.cmp_offset 取到的是普通函数，
        # 绑定方法调用会自动以实例填充 self（函数体不使用 self）
        return self.cmp_offset(10, 0, delta_rule, text, None, "X")

    def test_trailing_g_no_args_does_not_crash(self):
        """DeltaRule 以 "g" 结尾（无 count/value 参数）——旧实现 IndexError。"""
        pos_list = self._run("g")
        assert isinstance(pos_list, list) and len(pos_list) >= 1

    def test_g_with_count_only_does_not_crash(self):
        """DeltaRule = "g 3"（缺 value）——旧实现 IndexError。"""
        pos_list = self._run("g 3")
        assert isinstance(pos_list, list) and len(pos_list) >= 1

    def test_g_with_bad_number_does_not_crash(self):
        """DeltaRule = "g x y"（count/value 非数字）——旧实现 ValueError。"""
        pos_list = self._run("g x y")
        assert isinstance(pos_list, list) and len(pos_list) >= 1

    def test_valid_g_still_applies_kerning(self):
        """正常 "g 2 5"（2 个重复字距 5）行为不回归：pos 共 1+2 个。"""
        pos_list = self._run("g 2 5", text="ABC")
        assert len(pos_list) == 3
        assert pos_list[1] - pos_list[0] == pytest.approx(5)
        assert pos_list[2] - pos_list[1] == pytest.approx(5)

    def test_plain_numeric_offsets_still_work(self):
        """普通数值字距分支不回归。"""
        pos_list = self._run("2 3", text="ABC")
        assert len(pos_list) == 3
        assert pos_list[2] - pos_list[0] == pytest.approx(5)


# ---------------------------------------------------------------------------
# P1-6 convert_ofd_batch_async
# ---------------------------------------------------------------------------

def _make_ofd(tmp_path, name="a.ofd", payload=b"PK\x03\x04 not a real ofd"):
    p = tmp_path / name
    p.write_bytes(payload)
    return str(p)


class TestConvertOfdBatchAsync:
    def test_rejects_corrupt_zip_before_conversion(self, tmp_path, monkeypatch):
        """损坏 zip：预扫描拦截 → 400，且不触发转换（mock 断言未被调用）。"""

        async def impl(tmp_path, monkeypatch):
            bad = _make_ofd(tmp_path)

            async def _fail(path_in, path_out, crop=True):
                raise AssertionError("converter must not be called for corrupt zip")

            monkeypatch.setattr(invoice_merge_shared, "convert_ofd_to_pdf", _fail)

            with pytest.raises(HTTPException) as ei:
                await invoice_merge_shared.convert_ofd_batch_async(
                    str(tmp_path), [(0, bad)], ["a.ofd"]
                )
            assert ei.value.status_code == 400
            assert "不是有效的 OFD 文件" in ei.value.detail

        _run(impl(tmp_path, monkeypatch))

    def test_rejects_encrypted_with_friendly_message(self, tmp_path, monkeypatch):
        """加密 OFD：映射为 400 + 加密文案（而非笼统 500）。"""

        async def impl(tmp_path, monkeypatch):
            ofd = _make_ofd(tmp_path)

            def _enc(path):
                raise ofd_validator.OfdEncryptedError("encrypted")

            monkeypatch.setattr(invoice_merge_shared, "validate_ofd_zip", _enc)

            called = {"n": 0}

            async def _fail(path_in, path_out, crop=True):
                called["n"] += 1

            monkeypatch.setattr(invoice_merge_shared, "convert_ofd_to_pdf", _fail)

            with pytest.raises(HTTPException) as ei:
                await invoice_merge_shared.convert_ofd_batch_async(
                    str(tmp_path), [(0, ofd)], ["a.ofd"]
                )
            assert ei.value.status_code == 400
            assert "已加密" in ei.value.detail
            assert called["n"] == 0

        _run(impl(tmp_path, monkeypatch))

    def test_conversion_failure_cleans_intermediate(self, tmp_path, monkeypatch):
        """转换失败：fail-fast 400 + 目录内 invoice_*.pdf 中间产物被清理。"""

        async def impl(tmp_path, monkeypatch):
            ofd = _make_ofd(tmp_path)
            monkeypatch.setattr(
                invoice_merge_shared, "validate_ofd_zip", lambda path: None
            )

            async def _fail(path_in, path_out, crop=True):
                with open(path_out, "wb") as f:
                    f.write(b"partial")
                return False, "boom"

            monkeypatch.setattr(invoice_merge_shared, "convert_ofd_to_pdf", _fail)

            with pytest.raises(HTTPException) as ei:
                await invoice_merge_shared.convert_ofd_batch_async(
                    str(tmp_path), [(0, ofd)], ["a.ofd"]
                )
            assert ei.value.status_code == 400
            leftovers = [
                f for f in os.listdir(str(tmp_path))
                if f.startswith("invoice_") and f.endswith(".pdf")
            ]
            assert leftovers == []

        _run(impl(tmp_path, monkeypatch))

    def test_happy_path_produces_named_output(self, tmp_path, monkeypatch):
        """成功路径：产物按全局序号命名 invoice_001.pdf。"""

        async def impl(tmp_path, monkeypatch):
            ofd = _make_ofd(tmp_path)
            monkeypatch.setattr(
                invoice_merge_shared, "validate_ofd_zip", lambda path: None
            )

            async def _ok(path_in, path_out, crop=True):
                with open(path_out, "wb") as f:
                    f.write(b"%PDF-1.7 converted")
                return True, path_out

            monkeypatch.setattr(invoice_merge_shared, "convert_ofd_to_pdf", _ok)

            await invoice_merge_shared.convert_ofd_batch_async(
                str(tmp_path), [(0, ofd)], ["a.ofd"]
            )
            out = tmp_path / "invoice_001.pdf"
            assert out.exists()
            assert out.read_bytes().startswith(b"%PDF-1.7")

        _run(impl(tmp_path, monkeypatch))

    def test_global_index_survives_mixed_sequence(self, tmp_path, monkeypatch):
        """混合序列 [PDF, OFD]：OFD 全局序号 1 → 产物 invoice_002.pdf，
        不覆盖直存的 invoice_001.pdf（审计 #1 回归）。"""

        async def impl(tmp_path, monkeypatch):
            pdf_direct = tmp_path / "invoice_001.pdf"
            pdf_direct.write_bytes(b"%PDF-1.7 direct")
            ofd = _make_ofd(tmp_path, "b.ofd")
            monkeypatch.setattr(
                invoice_merge_shared, "validate_ofd_zip", lambda path: None
            )

            async def _ok(path_in, path_out, crop=True):
                with open(path_out, "wb") as f:
                    f.write(b"%PDF-1.7 converted")
                return True, path_out

            monkeypatch.setattr(invoice_merge_shared, "convert_ofd_to_pdf", _ok)

            await invoice_merge_shared.convert_ofd_batch_async(
                str(tmp_path), [(1, ofd)], ["b.ofd"]
            )
            assert pdf_direct.read_bytes() == b"%PDF-1.7 direct"
            assert (tmp_path / "invoice_002.pdf").exists()

        _run(impl(tmp_path, monkeypatch))

    def test_real_itinerary_ofd_end_to_end(self, tmp_path):
        """真实样本端到端（本机有样本时）：预扫描 → 转换 → 产物可打开。

        无候选字体环境下中文可能缺字形，但流程与产物有效性必须成立。
        """
        sample = r"C:\home\icreate\test\ofd-samples-20261009\1d1fd59e-4c13-4306-babe-1c61d1143ea1\input_001.ofd"
        if not os.path.exists(sample):
            pytest.skip("本机无行程单真实样本")
        import shutil

        ofd_copy = str(tmp_path / "input_001.ofd")
        shutil.copyfile(sample, ofd_copy)

        async def impl():
            await invoice_merge_shared.convert_ofd_batch_async(
                str(tmp_path), [(0, ofd_copy)], ["行程单.ofd"]
            )

        _run(impl())
        out = tmp_path / "invoice_001.pdf"
        assert out.exists() and out.stat().st_size > 1000
        import fitz

        doc = fitz.open(str(out))
        assert doc.page_count >= 1
        doc.close()

    def test_validate_zip_accepts_normal_zip(self, tmp_path):
        """预扫描回归：正常结构 zip 通过（规则 1-5 全绿基线）。"""
        p = tmp_path / "ok.ofd"
        with zipfile.ZipFile(p, "w") as zf:
            zf.writestr("OFD.xml", "<ofd:OFD xmlns:ofd='http://www.ofdspec.org/2016'/>")
            zf.writestr("Doc_0/Document.xml", "<doc/>")
        assert ofd_validator.validate_ofd_zip(str(p)) is None
