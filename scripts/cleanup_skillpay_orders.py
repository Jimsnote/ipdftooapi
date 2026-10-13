#!/usr/bin/env python3
"""skillpay 订单生命周期清理（review① R3；P0 设计 §4 规则 5）。

- created 且 created_at 超过 --created-hours（默认 48h，远超 15 分钟支付窗口）→ 删除；
- fulfilled 且 fulfilled_at 超过 --fulfilled-days（默认 90 天）→ 清 result_cache 大字段。

默认 dry-run 只列清单（对齐生产 find -delete 铁律：删前列清单人工审核）；
--execute 才真删。crontab 建议（错开 3 点 temp 清理）：
0 4 * * * cd /opt/ipdftoo/apps/api && .venv/bin/python scripts/cleanup_skillpay_orders.py --execute >> /var/log/skillpay_cleanup.log 2>&1

用法：
  python scripts/cleanup_skillpay_orders.py                 # dry-run
  python scripts/cleanup_skillpay_orders.py --execute       # 真删
  python scripts/cleanup_skillpay_orders.py --db /path/to/skillpay_orders.db
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.skillpay.orders import OrderStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="skillpay 订单清理（默认 dry-run）")
    parser.add_argument(
        "--db",
        default=os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data", "skillpay_orders.db",
        ),
        help="订单库路径（默认 apps/api/data/skillpay_orders.db）",
    )
    parser.add_argument("--created-hours", type=float, default=48.0,
                        help="created 订单保留小时数（默认 48）")
    parser.add_argument("--fulfilled-days", type=float, default=90.0,
                        help="fulfilled 订单 result_cache 保留天数（默认 90）")
    parser.add_argument("--execute", action="store_true",
                        help="真正执行删除（默认 dry-run 仅列清单）")
    args = parser.parse_args()

    store = OrderStore(args.db)
    stats = store.cleanup(
        created_hours=args.created_hours,
        fulfilled_days=args.fulfilled_days,
        execute=args.execute,
    )
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    mode = "EXECUTED" if stats["executed"] else "DRY-RUN（加 --execute 才真删）"
    print(f"[{mode}] 待删 created={stats['delete_created']} "
          f"待清 result_cache={stats['clear_result_cache']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
