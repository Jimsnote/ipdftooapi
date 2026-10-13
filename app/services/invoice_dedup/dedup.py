"""判重核心（纯函数，可单测）。

apps/web/src/lib/invoiceDedup/dedup.ts 的逐函数移植（golden tests 见
tests/test_invoice_dedup.py）：
- 批内 Map 先行，再比对历史台账；金额双缺省时跳过「可疑」维度；
- 同文件多行同键 → 后者标「文件内重复」。

设计依据：docs/2026-10-13_invoice-toolkit-pro-p0-design.md §7.2——
键归一化与判重全在服务端（升级即全员生效），skill 客户端只管解析。
"""
from dataclasses import dataclass, field
from datetime import date as _date
from typing import Dict, List, Optional

from app.services.invoice_dedup.keys import make_key

TICKET_STATUS_OK = "ok"
TICKET_STATUS_DUPLICATE = "duplicate"
TICKET_STATUS_SUSPICIOUS = "suspicious"
TICKET_STATUS_UNRECOGNIZED = "unrecognized"


@dataclass
class ParsedTicket:
    """单张被解析出的票（一个文件可能产出多张，如多票 PDF）。"""

    file_name: str
    file_ref: str
    code: Optional[str] = None
    number: Optional[str] = None
    amount: Optional[str] = None
    date: Optional[str] = None


@dataclass
class LedgerEntry:
    """历史台账条目（skill 侧本地 ledger 文件随请求携带）。"""

    key: str
    number: str
    code: Optional[str] = None
    amount: Optional[str] = None
    first_name: str = ""
    first_day: str = ""  # YYYY-MM-DD


@dataclass
class DedupRow(ParsedTicket):
    status: str = TICKET_STATUS_OK
    key: Optional[str] = None
    note: str = ""


def dedup_tickets(
    tickets: List[ParsedTicket],
    ledger: Dict[str, LedgerEntry],
) -> List[DedupRow]:
    """批内判重 + 台账比对。

    命中优先级（与 TS 版一致）：文件内重复 > 批内重复 > 历史台账；
    批内首见仅在「无任何命中」时写入 seen（台账命中不进批内 Map——
    同键第三张票会再次命中台账，行为与 TS 版逐分支对齐）。
    """
    seen: Dict[str, tuple] = {}  # key -> (批内首见行号, ticket)
    file_keys: Dict[str, set] = {}  # file_ref -> 该文件已见键集合
    rows: List[DedupRow] = []

    def judge(t: ParsedTicket, hit_amount: Optional[str], label: str):
        suspicious = bool(t.amount) and bool(hit_amount) and t.amount != hit_amount
        if suspicious:
            return (
                TICKET_STATUS_SUSPICIOUS,
                f"{label}，但金额不同（本方 ¥{t.amount} / 对方 ¥{hit_amount}），请人工复核",
            )
        return TICKET_STATUS_DUPLICATE, label

    def row_of(t: ParsedTicket, status: str, key: Optional[str], note: str) -> DedupRow:
        return DedupRow(
            file_name=t.file_name,
            file_ref=t.file_ref,
            code=t.code,
            number=t.number,
            amount=t.amount,
            date=t.date,
            status=status,
            key=key,
            note=note,
        )

    for i, t in enumerate(tickets):
        key = make_key(t.number, t.code)
        if not key:
            rows.append(row_of(t, TICKET_STATUS_UNRECOGNIZED, None, ""))
            continue

        # 文件内去重：同一文件的同键多行（同一笔票贴了多页按一次计）
        fk = file_keys.setdefault(t.file_ref, set())
        if key in fk:
            rows.append(row_of(t, TICKET_STATUS_DUPLICATE, key, "同一文件内重复出现"))
            continue
        fk.add(key)

        batch_hit = seen.get(key)
        ledger_hit = ledger.get(key)
        if batch_hit is not None:
            idx, first = batch_hit
            status, note = judge(t, first.amount, f"与本批第 {idx + 1} 行重复")
            rows.append(row_of(t, status, key, note))
        elif ledger_hit is not None:
            status, note = judge(
                t,
                ledger_hit.amount,
                f"历史台账中出现过（{ledger_hit.first_day}「{ledger_hit.first_name}」）",
            )
            rows.append(row_of(t, status, key, note))
        else:
            seen[key] = (i, t)
            rows.append(row_of(t, TICKET_STATUS_OK, key, ""))

    return rows


def to_ledger_entries(
    rows: List[DedupRow],
    today: Optional[str] = None,
) -> List[LedgerEntry]:
    """供台账沉淀：本次所有「识别成功」的票（正常/重复/可疑都沉淀，防跨批重报）。

    同键只留首条；today 注入便于测试（默认取本地日期，YYYY-MM-DD）。
    """
    out: List[LedgerEntry] = []
    seen: set = set()
    first_day = today or _date.today().isoformat()
    for r in rows:
        if not r.key or r.key in seen:
            continue
        seen.add(r.key)
        out.append(
            LedgerEntry(
                key=r.key,
                number=r.number or "",
                code=r.code,
                amount=r.amount,
                first_name=r.file_ref,
                first_day=first_day,
            )
        )
    return out
