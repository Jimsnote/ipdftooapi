"""SQLite 订单存取（P0 设计 §4；review① R2 原子抢占 + R3 生命周期清理）。

- WAL + busy_timeout=5000：单写者场景足够（预期量级远低于 SQLite 能力）；
- 每次操作独立连接（sqlite3 连接轻量，天然线程安全），避免跨线程共享游标；
- data/ 目录被根 .gitignore 排除（apps/api/data/），部署 git archive 不解包不覆盖。
"""
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from app.services.skillpay.payment import CST

_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  out_trade_no TEXT NOT NULL UNIQUE,
  trade_no     TEXT UNIQUE,
  op           TEXT NOT NULL,
  resource_id  TEXT NOT NULL,
  amount       TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'created',
  task_id      TEXT,
  result_cache TEXT,
  file_count   INTEGER,
  created_at   TEXT NOT NULL,
  paid_at      TEXT,
  fulfilled_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_trade ON orders(trade_no);
"""

STATUS_CREATED = "created"
STATUS_VERIFIED = "verified"
STATUS_FULFILLED = "fulfilled"


def _now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


@dataclass
class Order:
    out_trade_no: str
    op: str
    resource_id: str
    amount: str
    status: str
    trade_no: Optional[str] = None
    task_id: Optional[str] = None
    result_cache: Optional[str] = None
    file_count: Optional[int] = None
    created_at: Optional[str] = None
    paid_at: Optional[str] = None
    fulfilled_at: Optional[str] = None

    def result_body(self) -> Optional[dict]:
        import json

        if not self.result_cache:
            return None
        try:
            return json.loads(self.result_cache)
        except ValueError:
            return None


class OrderStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        parent = os.path.dirname(os.path.abspath(db_path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")  # 持久属性，写一次即可
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    @staticmethod
    def _to_order(row: Optional[sqlite3.Row]) -> Optional[Order]:
        if row is None:
            return None
        return Order(
            out_trade_no=row["out_trade_no"],
            op=row["op"],
            resource_id=row["resource_id"],
            amount=row["amount"],
            status=row["status"],
            trade_no=row["trade_no"],
            task_id=row["task_id"],
            result_cache=row["result_cache"],
            file_count=row["file_count"],
            created_at=row["created_at"],
            paid_at=row["paid_at"],
            fulfilled_at=row["fulfilled_at"],
        )

    # -- 基本读写 -----------------------------------------------------------

    def create_order(
        self,
        out_trade_no: str,
        op: str,
        resource_id: str,
        amount: str,
        file_count: int,
        task_id: Optional[str] = None,
    ) -> None:
        """task_id：文件批 probe 持久化的上传文件目录（P4 resume 模式恢复源）。"""
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO orders (out_trade_no, op, resource_id, amount, status, file_count, task_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (out_trade_no, op, resource_id, amount, STATUS_CREATED, file_count, task_id, _now_iso()),
            )

    def get_order(self, out_trade_no: str) -> Optional[Order]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE out_trade_no = ?", (out_trade_no,)
            ).fetchone()
        return self._to_order(row)

    def find_by_trade_no(self, trade_no: str) -> Optional[Order]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE trade_no = ?", (trade_no,)
            ).fetchone()
        return self._to_order(row)

    # -- 状态机（§4 规则 2/2b/3/4，R2 原子抢占） ---------------------------

    def claim_for_verify(self, out_trade_no: str, trade_no: str) -> bool:
        """原子抢占：受影响行数=1 才允许执行业务；=0 即并发重入。

        review① R2：verified→fulfilled 之间的并发重入靠本方法的
        `WHERE trade_no IS NULL` 条件杜绝双跑业务。
        """
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE orders SET trade_no = ?, status = ?, paid_at = ?"
                " WHERE out_trade_no = ? AND trade_no IS NULL",
                (trade_no, STATUS_VERIFIED, _now_iso(), out_trade_no),
            )
            return cur.rowcount == 1

    def release_claim(self, out_trade_no: str) -> None:
        """业务执行失败后的恢复：订单回到 created/无 trade_no，买家可携凭证重试。

        （避免 dispatch 异常把订单卡死在 verified→永久 409。）
        """
        with self._connect() as conn:
            conn.execute(
                "UPDATE orders SET trade_no = NULL, status = ?, paid_at = NULL"
                " WHERE out_trade_no = ? AND status = ?",
                (STATUS_CREATED, out_trade_no, STATUS_VERIFIED),
            )

    def mark_fulfilled(self, out_trade_no: str, task_id: Optional[str], result_cache: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE orders SET status = ?, task_id = ?, result_cache = ?, fulfilled_at = ?"
                " WHERE out_trade_no = ?",
                (STATUS_FULFILLED, task_id, result_cache, _now_iso(), out_trade_no),
            )

    # -- 生命周期清理（review① R3） ----------------------------------------

    def cleanup(
        self,
        *,
        created_hours: float = 48.0,
        fulfilled_days: float = 90.0,
        execute: bool = False,
        now: Optional[datetime] = None,
    ) -> dict:
        """created 超时删除；fulfilled 超期清 result_cache 大字段。

        默认 dry-run（只统计与列清单，不改动）——对齐生产 find -delete 铁律：
        删前列清单人工审核；--execute 才真删（scripts/cleanup_skillpay_orders.py）。
        """
        now = now or datetime.now(CST)
        created_cutoff = (now - timedelta(hours=created_hours)).isoformat(timespec="seconds")
        fulfilled_cutoff = (now - timedelta(days=fulfilled_days)).isoformat(timespec="seconds")

        with self._connect() as conn:
            stale_created = [
                r["out_trade_no"]
                for r in conn.execute(
                    "SELECT out_trade_no FROM orders WHERE status = ? AND created_at < ?"
                    " ORDER BY created_at",
                    (STATUS_CREATED, created_cutoff),
                )
            ]
            stale_fulfilled = [
                r["out_trade_no"]
                for r in conn.execute(
                    "SELECT out_trade_no FROM orders WHERE status = ? AND fulfilled_at < ?"
                    " AND result_cache IS NOT NULL ORDER BY fulfilled_at",
                    (STATUS_FULFILLED, fulfilled_cutoff),
                )
            ]
            stats = {
                "delete_created": len(stale_created),
                "created_out_trade_nos": stale_created,
                "clear_result_cache": len(stale_fulfilled),
                "fulfilled_out_trade_nos": stale_fulfilled,
                "executed": False,
            }
            if execute:
                if stale_created:
                    conn.executemany(
                        "DELETE FROM orders WHERE out_trade_no = ? AND status = ?",
                        [(no, STATUS_CREATED) for no in stale_created],
                    )
                if stale_fulfilled:
                    conn.executemany(
                        "UPDATE orders SET result_cache = NULL WHERE out_trade_no = ?",
                        [(no,) for no in stale_fulfilled],
                    )
                stats["executed"] = True
        return stats
