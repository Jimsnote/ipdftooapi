"""发票批量查重服务包（apps/web/src/lib/invoiceDedup 的 Python 移植）。

- keys.py  键归一化与号码语义校验（TS 逐函数对应移植，golden tests 护航）
- dedup.py 判重核心（批内 Map + 台账比对 + 金额不一致标可疑）

设计依据：docs/2026-10-13_invoice-toolkit-pro-p0-design.md §7
（服务端零持久化票面数据：订单表只存 op/resource_id/计数。）
"""
from app.services.invoice_dedup.dedup import (  # noqa: F401
    DedupRow,
    LedgerEntry,
    ParsedTicket,
    dedup_tickets,
    to_ledger_entries,
)
from app.services.invoice_dedup.keys import (  # noqa: F401
    add_amounts,
    clean_amount,
    cn_date_from_text,
    date_from_file_name,
    excel_text,
    is_classic_code,
    is_classic_no,
    is_valid_e_invoice_no,
    make_key,
    normalize_text,
)
