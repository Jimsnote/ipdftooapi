import json
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers import survey as survey_router
from scripts.export_survey import export_csv


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(survey_router, "DB_PATH", str(tmp_path / "survey.db"))
    return TestClient(app)


def payload(**overrides):
    data = {
        "frequency": "这是第一次使用",
        "features": ["发票合并打印"],
        "source": "百度搜索",
        "pricing_model": "要看具体功能再决定",
        "price_range": "19.9 元",
        "feature_request": "希望支持更多批量功能",
        "task_outcome": "一次就成功，结果符合预期",
        "issue_description": "",
        "usage_scenario": "个人报销或日常办公",
        "contact": "",
        "feature_other": "",
        "source_other": "",
        "scenario_other": "",
        "page_path": "/invoice-merge/result",
        "client_id": uuid.uuid4().hex,
        "schema_version": "v1",
        "elapsed_ms": 5000,
        "website": "",
    }
    data.update(overrides)
    return data


def response_count(client):
    conn = sqlite3.connect(survey_router.DB_PATH)
    try:
        return conn.execute("SELECT count(*) FROM survey_responses").fetchone()[0]
    finally:
        conn.close()


def test_submit_valid_survey_and_export_csv(client, tmp_path):
    response = client.post("/api/v1/survey/submit", json=payload(contact="user@example.com"))
    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}

    conn = sqlite3.connect(survey_router.DB_PATH)
    row = conn.execute("SELECT * FROM survey_responses").fetchone()
    columns = [item[1] for item in conn.execute("PRAGMA table_info(survey_responses)")]
    conn.close()
    assert row is not None
    assert "task_id" not in columns
    assert "filename" not in columns
    assert "ip" not in columns
    assert json.loads(row[3]) == ["发票合并打印"]

    output = tmp_path / "survey.csv"
    assert export_csv(survey_router.DB_PATH, str(output)) == 1
    content = output.read_text(encoding="utf-8-sig")
    assert "发票合并打印" in content
    assert "user@example.com" in content


def test_duplicate_client_is_not_inserted_twice(client):
    body = payload()
    assert client.post("/api/v1/survey/submit", json=body).status_code == 200
    assert client.post("/api/v1/survey/submit", json=body).status_code == 200
    assert response_count(client) == 1


@pytest.mark.parametrize("override", [{"website": "spam"}, {"elapsed_ms": 1000}])
def test_honeypot_and_too_fast_submission_are_not_stored(client, override):
    response = client.post("/api/v1/survey/submit", json=payload(**override))
    assert response.status_code == 200
    with pytest.raises(sqlite3.OperationalError):
        response_count(client)


@pytest.mark.parametrize(
    "override",
    [
        {"features": []},
        {"features": ["不存在的功能"]},
        {"source": "未知来源"},
        {"feature_request": "长" * 501},
        {"task_id": "must-not-be-accepted"},
    ],
)
def test_invalid_or_unsupported_fields_rejected(client, override):
    response = client.post("/api/v1/survey/submit", json=payload(**override))
    assert response.status_code == 422


def test_request_body_over_8kb_rejected_before_parsing(client):
    response = client.post(
        "/api/v1/survey/submit",
        content=b" " * 8193,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["detail"] == "问卷内容过长"
