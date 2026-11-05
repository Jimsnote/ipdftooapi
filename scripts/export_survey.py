import argparse
import csv
import json
import os
import sqlite3

from app.config import settings


_COLUMNS = [
    "response_id", "submitted_at", "frequency", "features", "source",
    "pricing_model", "price_range", "feature_request", "task_outcome",
    "issue_description", "usage_scenario", "contact", "feature_other",
    "source_other", "scenario_other", "page_path", "client_id", "schema_version",
]


def export_csv(db_path: str, output_path: str) -> int:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM survey_responses ORDER BY submitted_at"
        ).fetchall()
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_COLUMNS)
        writer.writeheader()
        for row in rows:
            item = dict(row)
            item["features"] = "、".join(json.loads(item.pop("features_json")))
            writer.writerow(item)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="导出匿名问卷 CSV")
    parser.add_argument("--db-path", default=settings.SURVEY_DB_PATH)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    count = export_csv(args.db_path, args.output)
    print(f"已导出 {count} 条问卷到 {args.output}")


if __name__ == "__main__":
    main()
