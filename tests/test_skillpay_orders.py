"""skillpay orders 模块测试（P0 设计 §9 单测-订单）。

状态机四规则（§4）+ review① R2 原子抢占 + R3 生命周期清理（dry-run 默认）。
"""
import json
from datetime import datetime, timedelta

import pytest

from app.services.skillpay.orders import (
    STATUS_CREATED,
    STATUS_FULFILLED,
    STATUS_VERIFIED,
    OrderStore,
)
from app.services.skillpay.payment import CST


@pytest.fixture()
def store(tmp_path):
    return OrderStore(str(tmp_path / "orders.db"))


def _create(store, out_trade_no="SK1" + "0" * 30, op="merge", amount="0.01"):
    store.create_order(out_trade_no, op, "M" + "1" * 31, amount, 5)
    return out_trade_no


class TestBasics:
    def test_create_and_get_roundtrip(self, store):
        no = _create(store, op="extract", amount="1.99")
        order = store.get_order(no)
        assert order is not None
        assert order.out_trade_no == no
        assert order.op == "extract"
        assert order.amount == "1.99"
        assert order.status == STATUS_CREATED
        assert order.trade_no is None
        assert order.file_count == 5
        assert order.created_at is not None

    def test_get_missing_returns_none(self, store):
        assert store.get_order("SK-nonexistent") is None
        assert store.find_by_trade_no("T-nonexistent") is None

    def test_duplicate_out_trade_no_rejected(self, store):
        no = _create(store)
        with pytest.raises(Exception):
            store.create_order(no, "merge", "M2", "0.01", 1)

    def test_persistence_across_instances(self, tmp_path):
        db = str(tmp_path / "orders.db")
        s1 = OrderStore(db)
        _create(s1, "SK" + "9" * 30)
        s2 = OrderStore(db)  # 同一库文件新实例（schema IF NOT EXISTS 幂等）
        assert s2.get_order("SK" + "9" * 30) is not None


class TestClaimStateMachine:
    def test_claim_success_then_reentry_fails(self, store):
        no = _create(store)
        assert store.claim_for_verify(no, "T1") is True
        order = store.get_order(no)
        assert order.status == STATUS_VERIFIED
        assert order.trade_no == "T1"
        assert order.paid_at is not None
        # 第二个并发请求重入 → False（R2：杜绝双跑业务）
        assert store.claim_for_verify(no, "T2") is False
        assert store.get_order(no).trade_no == "T1"

    def test_claim_missing_order_returns_false(self, store):
        assert store.claim_for_verify("SK-nope", "T1") is False

    def test_release_then_reclaim(self, store):
        no = _create(store)
        assert store.claim_for_verify(no, "T1") is True
        store.release_claim(no)  # dispatch 异常恢复路径
        order = store.get_order(no)
        assert order.status == STATUS_CREATED
        assert order.trade_no is None
        assert order.paid_at is None
        # 买家携同一凭证重试 → 重新抢占成功
        assert store.claim_for_verify(no, "T1") is True

    def test_mark_fulfilled_and_find_by_trade_no(self, store):
        no = _create(store, op="dedup")
        store.claim_for_verify(no, "T9")
        body = {"op": "dedup", "summary": {"total": 3}, "out_trade_no": no, "trade_no": "T9"}
        store.mark_fulfilled(no, task_id=None, result_cache=json.dumps(body))
        order = store.find_by_trade_no("T9")
        assert order is not None
        assert order.status == STATUS_FULFILLED
        assert order.fulfilled_at is not None
        assert order.result_body() == body

    def test_result_body_corrupt_cache_returns_none(self, store):
        no = _create(store)
        store.claim_for_verify(no, "T1")
        store.mark_fulfilled(no, None, "{not json")
        assert store.get_order(no).result_body() is None

    def test_trade_no_unique_across_orders(self, store):
        no1 = _create(store, "SK" + "1" * 30)
        no2 = _create(store, "SK" + "2" * 30)
        assert store.claim_for_verify(no1, "T1") is True
        # 同一 trade_no 不能占第二个订单（唯一约束语义）
        with pytest.raises(Exception):
            store.claim_for_verify(no2, "T1")


class TestCleanup:
    def _seed(self, store, now):
        # 一条过期的 created（55 小时前）
        old_created = "SK" + "A" * 30
        store.create_order(old_created, "merge", "M" + "a" * 31, "0.01", 1)
        with store._connect() as conn:
            conn.execute(
                "UPDATE orders SET created_at = ? WHERE out_trade_no = ?",
                ((now - timedelta(hours=55)).isoformat(timespec="seconds"), old_created),
            )
        # 一条新鲜的 created（1 小时前）
        fresh_created = "SK" + "B" * 30
        store.create_order(fresh_created, "merge", "M" + "b" * 31, "0.01", 1)
        with store._connect() as conn:
            conn.execute(
                "UPDATE orders SET created_at = ? WHERE out_trade_no = ?",
                ((now - timedelta(hours=1)).isoformat(timespec="seconds"), fresh_created),
            )
        # 一条过期的 fulfilled（100 天前，带 result_cache）
        old_done = "SK" + "C" * 30
        store.create_order(old_done, "dedup", "D" + "c" * 31, "0.01", 1)
        store.claim_for_verify(old_done, "TOLD")
        store.mark_fulfilled(old_done, None, '{"ok":1}')
        with store._connect() as conn:
            conn.execute(
                "UPDATE orders SET fulfilled_at = ? WHERE out_trade_no = ?",
                ((now - timedelta(days=100)).isoformat(timespec="seconds"), old_done),
            )
        # 一条新鲜的 fulfilled（1 天前）
        fresh_done = "SK" + "E" * 30
        store.create_order(fresh_done, "dedup", "D" + "e" * 31, "0.01", 1)
        store.claim_for_verify(fresh_done, "TNEW")
        store.mark_fulfilled(fresh_done, None, '{"ok":2}')
        return old_created, fresh_created, old_done, fresh_done

    def test_dry_run_lists_without_deleting(self, store):
        now = datetime.now(CST)
        old_created, fresh_created, old_done, fresh_done = self._seed(store, now)
        stats = store.cleanup(now=now)
        assert stats["executed"] is False
        assert stats["delete_created"] == 1
        assert stats["created_out_trade_nos"] == [old_created]
        assert stats["clear_result_cache"] == 1
        assert stats["fulfilled_out_trade_nos"] == [old_done]
        # 未执行任何删除
        assert store.get_order(old_created) is not None
        assert store.get_order(old_done).result_cache == '{"ok":1}'

    def test_execute_deletes_and_clears(self, store):
        now = datetime.now(CST)
        old_created, fresh_created, old_done, fresh_done = self._seed(store, now)
        stats = store.cleanup(execute=True, now=now)
        assert stats["executed"] is True
        assert store.get_order(old_created) is None
        assert store.get_order(fresh_created) is not None
        assert store.get_order(old_done) is not None  # fulfilled 行保留
        assert store.get_order(old_done).result_cache is None  # 大字段已清
        assert store.get_order(fresh_done).result_cache == '{"ok":2}'
