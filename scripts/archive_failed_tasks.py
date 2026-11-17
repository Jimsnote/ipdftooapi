import argparse
import json
import os
import re
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

TASK_PATTERN = re.compile(
    r"\btask\s+(?P<task_id>[0-9a-f]{8}-[0-9a-f-]{27})\s+(?:rejected/)?failed:\s*(?P<message>.*)"
)
REQUEST_PATTERN = re.compile(
    r"Request failed request_id=(?P<request_id>[0-9a-f]+) method=(?P<method>\S+) "
    r"path=(?P<path>\S+) status=(?P<status>\d+)"
)
CST = timezone(timedelta(hours=8))
DEFAULT_LOGS = [
    Path("/home/ubuntu/.pm2/logs/ipdftoo-api-out.log"),
    Path("/home/ubuntu/.pm2/logs/ipdftoo-api-error.log"),
]
MAX_TASK_BYTES = 64 * 1024 * 1024
MAX_DAILY_BYTES = 512 * 1024 * 1024


def parse_timestamp(line: str) -> datetime | None:
    try:
        return datetime.strptime(line[:23], "%Y-%m-%d %H:%M:%S,%f").replace(tzinfo=CST)
    except ValueError:
        return None


def parse_logs(log_paths: list[Path], target_date: date) -> tuple[dict, list]:
    tasks: dict[str, dict] = {}
    requests: dict[str, dict] = {}
    prefix = target_date.isoformat()

    for log_path in log_paths:
        if not log_path.exists():
            continue
        with log_path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.startswith(prefix):
                    continue
                timestamp = parse_timestamp(line)
                task_match = TASK_PATTERN.search(line)
                if task_match:
                    task_id = task_match.group("task_id")
                    task = tasks.setdefault(
                        task_id,
                        {
                            "task_id": task_id,
                            "first_seen": line[:23],
                            "messages": [],
                        },
                    )
                    task["last_seen"] = line[:23]
                    if len(task["messages"]) < 10:
                        task["messages"].append(task_match.group("message")[:2000])
                request_match = REQUEST_PATTERN.search(line)
                if request_match and timestamp:
                    item = request_match.groupdict()
                    item["status"] = int(item["status"])
                    item["time"] = line[:23]
                    item["_timestamp"] = timestamp
                    requests[item["request_id"]] = item

    for request in requests.values():
        request_time = request.pop("_timestamp")
        candidates = []
        for task in tasks.values():
            task_time = parse_timestamp(task["last_seen"])
            if task_time and timedelta(0) <= request_time - task_time <= timedelta(seconds=10):
                candidates.append((task_time, task))
        if candidates:
            task = max(candidates, key=lambda item: item[0])[1]
            task.update(
                request_id=request["request_id"],
                endpoint=request["path"],
                status=request["status"],
            )

    ordered_requests = sorted(requests.values(), key=lambda item: item["time"])
    return tasks, ordered_requests


def is_original_file(path: Path) -> bool:
    name = path.name.lower()
    if name.startswith("input"):
        return True
    return name.startswith("invoice_") and name != "merged_invoices.pdf"


def copy_originals(task_dir: Path, destination: Path, remaining_bytes: int) -> tuple[list, list, int]:
    archived = []
    skipped = []
    used_bytes = 0
    if not task_dir.is_dir():
        return archived, [{"reason": "任务目录已清理或不存在"}], used_bytes

    destination.mkdir(parents=True, exist_ok=True)
    originals = sorted(path for path in task_dir.iterdir() if path.is_file() and is_original_file(path))
    if not originals:
        skipped.append({"reason": "未找到可归档的原始文件"})

    for source in originals:
        size = source.stat().st_size
        if size > MAX_TASK_BYTES or used_bytes + size > MAX_TASK_BYTES:
            skipped.append({"name": source.name, "size": size, "reason": "超过单任务归档上限"})
            continue
        if size > remaining_bytes - used_bytes:
            skipped.append({"name": source.name, "size": size, "reason": "超过每日归档上限"})
            continue
        target = destination / source.name
        if not target.exists():
            temporary = destination / f".{source.name}.tmp"
            shutil.copy2(source, temporary)
            os.chmod(temporary, 0o600)
            os.replace(temporary, target)
        archived.append({"name": source.name, "size": size})
        used_bytes += size
    return archived, skipped, used_bytes


def prune_archive(archive_root: Path, target_date: date, retention_days: int) -> list[str]:
    removed = []
    cutoff = target_date - timedelta(days=retention_days - 1)
    if not archive_root.exists():
        return removed
    for path in archive_root.iterdir():
        if not path.is_dir():
            continue
        try:
            archived_date = date.fromisoformat(path.name)
        except ValueError:
            continue
        if archived_date < cutoff:
            shutil.rmtree(path)
            removed.append(path.name)
    return removed


def archive_failures(
    target_date: date,
    archive_root: Path,
    temp_root: Path,
    log_paths: list[Path],
    retention_days: int = 30,
    dry_run: bool = False,
) -> dict:
    tasks, requests = parse_logs(log_paths, target_date)
    daily_root = archive_root / target_date.isoformat()
    task_results = []
    total_bytes = 0

    for task_id, task in sorted(tasks.items()):
        archived, skipped, used_bytes = [], [], 0
        if not dry_run:
            archived, skipped, used_bytes = copy_originals(
                temp_root / task_id,
                daily_root / task_id,
                MAX_DAILY_BYTES - total_bytes,
            )
        total_bytes += used_bytes
        task_results.append(
            {
                **task,
                "archived_files": archived,
                "skipped_files": skipped,
                "archived_bytes": used_bytes,
            }
        )

    summary = {
        "date": target_date.isoformat(),
        "generated_at": datetime.now(CST).isoformat(timespec="seconds"),
        "request_failure_count": len(requests),
        "task_failure_count": len(task_results),
        "archived_task_count": sum(bool(task["archived_files"]) for task in task_results),
        "archived_bytes": total_bytes,
        "request_failures": requests,
        "tasks": task_results,
        "dry_run": dry_run,
    }

    if not dry_run:
        daily_root.mkdir(parents=True, exist_ok=True)
        os.chmod(daily_root, 0o700)
        summary_path = daily_root / "summary.json"
        temporary = daily_root / ".summary.json.tmp"
        temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, summary_path)
        summary["pruned_dates"] = prune_archive(archive_root, target_date, retention_days)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="归档 iPDFToo 失败任务及原始文件")
    parser.add_argument("--date", type=date.fromisoformat)
    parser.add_argument("--archive-root", type=Path, default=Path("/opt/ipdftoo/failure-archive"))
    parser.add_argument("--temp-root", type=Path, default=Path("/opt/ipdftoo/apps/api/temp"))
    parser.add_argument("--log", dest="logs", type=Path, action="append")
    parser.add_argument("--retention-days", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.retention_days < 1:
        parser.error("--retention-days 必须大于 0")

    target_date = args.date or (datetime.now(CST).date() - timedelta(days=1))
    summary = archive_failures(
        target_date,
        args.archive_root,
        args.temp_root,
        args.logs or DEFAULT_LOGS,
        args.retention_days,
        args.dry_run,
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    os.umask(0o077)
    main()
