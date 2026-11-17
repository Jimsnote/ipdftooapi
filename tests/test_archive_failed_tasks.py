import json
from datetime import date
from pathlib import Path

from scripts.archive_failed_tasks import archive_failures

TASK_ID = "12345678-1234-1234-1234-123456789abc"


def _write_log(path: Path) -> None:
    path.write_text(
        "\n".join(
            [
                f"2026-11-16 10:00:00,000 - app.routers.pdf - ERROR - Merge task {TASK_ID} failed: KeyError('/Subtype')",
                "2026-11-16 10:00:00,100 - app.main - WARNING - Request failed request_id=0123456789abcdef method=POST path=/api/v1/pdf/merge status=500",
                "2026-11-15 10:00:00,000 - app.routers.pdf - ERROR - Merge task 00000000-0000-0000-0000-000000000000 failed: old",
            ]
        ),
        encoding="utf-8",
    )


def test_archive_failure_with_original_only(tmp_path):
    log = tmp_path / "api.log"
    _write_log(log)
    temp_root = tmp_path / "temp"
    task_dir = temp_root / TASK_ID
    task_dir.mkdir(parents=True)
    (task_dir / "input_0.pdf").write_bytes(b"original")
    (task_dir / "merged.pdf").write_bytes(b"output")
    (task_dir / "upload_names.txt").write_text("private.pdf", encoding="utf-8")
    archive_root = tmp_path / "archive"

    summary = archive_failures(date(2026, 11, 16), archive_root, temp_root, [log])

    assert summary["request_failure_count"] == 1
    assert summary["task_failure_count"] == 1
    assert summary["archived_task_count"] == 1
    task = summary["tasks"][0]
    assert task["request_id"] == "0123456789abcdef"
    assert task["status"] == 500
    assert task["archived_files"] == [{"name": "input_0.pdf", "size": 8}]
    archived_dir = archive_root / "2026-11-16" / TASK_ID
    assert (archived_dir / "input_0.pdf").read_bytes() == b"original"
    assert not (archived_dir / "merged.pdf").exists()
    assert not (archived_dir / "upload_names.txt").exists()
    saved_summary = json.loads((archive_root / "2026-11-16" / "summary.json").read_text(encoding="utf-8"))
    assert saved_summary["tasks"][0]["endpoint"] == "/api/v1/pdf/merge"

    archive_failures(date(2026, 11, 16), archive_root, temp_root, [log])
    assert list(archived_dir.iterdir()) == [archived_dir / "input_0.pdf"]


def test_prunes_only_expired_date_directories(tmp_path):
    archive_root = tmp_path / "archive"
    (archive_root / "2026-10-01").mkdir(parents=True)
    (archive_root / "2026-11-01").mkdir()
    (archive_root / "notes").mkdir()

    summary = archive_failures(date(2026, 11, 16), archive_root, tmp_path / "temp", [])

    assert summary["pruned_dates"] == ["2026-10-01"]
    assert (archive_root / "2026-11-01").exists()
    assert (archive_root / "notes").exists()


def test_dry_run_does_not_create_archive(tmp_path):
    log = tmp_path / "api.log"
    _write_log(log)
    archive_root = tmp_path / "archive"

    summary = archive_failures(date(2026, 11, 16), archive_root, tmp_path / "temp", [log], dry_run=True)

    assert summary["task_failure_count"] == 1
    assert summary["archived_bytes"] == 0
    assert not archive_root.exists()
