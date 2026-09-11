# -*- coding: utf-8 -*-
"""签证表回填路由层测试（TestClient，HANDOFF §五.4 的 422 映射部分）。

200 断言：PDF 魔数 / %%EOF 结尾 / Content-Type / Content-Disposition。
422 三分支：未知模板 / 非法 SOM / 超长值，detail 必须可读且不回显字段值。
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

TEMPLATE_ID = "imm5257e"

SAMPLE_VALUES = {
    "Page1/PersonalDetails/Name/FamilyName": "ZHANG",
    "Page1/PersonalDetails/Name/GivenName": "San",
    "Page1/PersonalDetails/DOBYear": "1990",
    "Page1/PersonalDetails/DOBMonth": "01",
    "Page1/PersonalDetails/DOBDay": "15",
    "Page1/PersonalDetails/PlaceBirthCity": "Beijing",
    "Page1/PersonalDetails/PlaceBirthCountry": "CHINA",
}


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def test_api_fill_success(client: TestClient):
    resp = client.post(f"/api/v1/visa/fill", json={"template": TEMPLATE_ID, "values": SAMPLE_VALUES})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/pdf")
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert f"{TEMPLATE_ID}-filled.pdf" in resp.headers.get("content-disposition", "")
    body = resp.content
    assert body[:5] == b"%PDF-", "响应不是 PDF"
    assert body.rstrip().endswith(b"%%EOF"), "响应未以 %%EOF 结尾"
    assert len(body) > 1_300_000, "产出体积异常（应约 1.45MB 增量更新产物）"


def test_api_fill_unknown_template_422(client: TestClient):
    resp = client.post(
        "/api/v1/visa/fill",
        json={"template": "imm9999x", "values": SAMPLE_VALUES},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]


def test_api_fill_invalid_som_422(client: TestClient):
    resp = client.post(
        "/api/v1/visa/fill",
        json={
            "template": TEMPLATE_ID,
            "values": {"Page1/Totally/Unknown/Path": "x"},
        },
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail
    # 不回显用户输入（防注入面收敛）
    assert "Totally" not in detail


def test_api_fill_value_too_long_422(client: TestClient):
    resp = client.post(
        "/api/v1/visa/fill",
        json={
            "template": TEMPLATE_ID,
            "values": {"Page1/PersonalDetails/Name/FamilyName": "A" * 501},
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]


def test_api_fill_control_chars_422(client: TestClient):
    """控制字符必须 422 干净拒绝（Phase 1-2 验收补强，防 ParseError→500）。"""
    resp = client.post(
        "/api/v1/visa/fill",
        json={
            "template": TEMPLATE_ID,
            "values": {"Page1/PersonalDetails/Name/FamilyName": "A\x00B"},
        },
    )
    assert resp.status_code == 422, "控制字符输入不应产生 500"
    assert resp.json()["detail"]
