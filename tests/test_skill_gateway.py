"""skill_gateway 集成测试（P0 设计 §9 集成层）。

用独立 FastAPI 应用挂载被测路由（不导入整个 app.main，避免拖起 OCR 等重依赖）；
AlipayClient 以桩注入（真实网关调用属 T2 沙箱联调），订单库指向 tmp_path。

覆盖：402+Payment-Needed 出账单 / 账单签名可验 / 伪 Proof → 402 新账单 /
verify 成功 → 200+异步履约 / 同 trade_no 重发 → already_fulfilled（不重验） /
五项业务校验失败 → 402 / 原子抢占并发重入 → 409 / dispatch 异常 → 释放抢占可重试 /
Content-Type 双模式 / 限额与文件头预检 / 下载端点。
"""
import base64
import json
import uuid as uuid_mod

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config as config_mod
from app.routers import skill_gateway
from app.services.skillpay import (
    AlipayGatewayError,
    VerifyResult,
    build_sign_content,
    decode_payment_needed,
    rsa2_verify,
)

URL = "/api/v1/skill/invoice"

NO1 = "24312000000012345678"
NO2 = "26112000001182262471"


@pytest.fixture(scope="module")
def keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_b64 = "".join(
        private.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
        .strip()
        .splitlines()[1:-1]
    )
    public_b64 = "".join(
        private.public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        .decode()
        .strip()
        .splitlines()[1:-1]
    )
    return {"private": private, "private_b64": private_b64, "public_b64": public_b64}


class StubClient:
    def __init__(self):
        self.verify_calls = []
        self.fulfill_calls = []
        self.verify_result: VerifyResult | None = None
        self.verify_error: AlipayGatewayError | None = None

    def verify_payment(self, payment_proof, trade_no, client_session):
        self.verify_calls.append((payment_proof, trade_no, client_session))
        if self.verify_error:
            raise self.verify_error
        assert self.verify_result is not None
        return self.verify_result

    def fulfillment_confirm(self, trade_no):
        self.fulfill_calls.append(trade_no)
        return True


@pytest.fixture()
def stub(keypair, tmp_path, monkeypatch):
    skill_gateway._reset_singletons()
    monkeypatch.setattr(skill_gateway, "ORDER_DB_PATH", str(tmp_path / "orders.db"))
    monkeypatch.setattr(config_mod.settings, "SKILLPAY_SANDBOX", True)
    monkeypatch.setattr(config_mod.settings, "SKILLPAY_APP_ID", "9021000169635550")
    monkeypatch.setattr(config_mod.settings, "SKILLPAY_PRIVATE_KEY", keypair["private_b64"])
    monkeypatch.setattr(config_mod.settings, "SKILLPAY_ALIPAY_PUBLIC_KEY", keypair["public_b64"])
    monkeypatch.setattr(config_mod.settings, "SKILLPAY_SELLER_ID", "2088721115842304")
    monkeypatch.setattr(config_mod.settings, "SKILLPAY_SERVICE_ID", "api_mock_service_id")
    monkeypatch.setattr(config_mod.settings, "SKILLPAY_AMOUNT", "0.01")
    stub_client = StubClient()
    monkeypatch.setattr(skill_gateway, "_get_alipay_client", lambda: stub_client)
    yield stub_client
    skill_gateway._reset_singletons()


@pytest.fixture()
def client(stub, monkeypatch, tmp_path):
    monkeypatch.setattr(skill_gateway, "TEMP_DIR", str(tmp_path / "temp"))
    app = FastAPI()
    app.include_router(skill_gateway.router, prefix="/api/v1/skill")
    # raise_server_exceptions=False：dispatch 异常路径测 500 响应而非抛穿透
    return TestClient(app, raise_server_exceptions=False)


def _dedup_body(n: int = 2) -> dict:
    return {
        "op": "dedup",
        "tickets": [
            {"fileRef": f"{i}.xml", "number": NO1 if i == 0 else NO2, "amount": "270.00"}
            for i in range(n)
        ],
        "ledger": [],
    }


def _proof(trade_no: str, payment_proof: str = "pp123", client_session: str = "cs123") -> str:
    raw = json.dumps(
        {
            "protocol": {"payment_proof": payment_proof, "trade_no": trade_no},
            "method": {"client_session": client_session},
        }
    ).encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _ok_verify_result(resp_json: dict) -> VerifyResult:
    return VerifyResult(
        trade_no="T-OK-1",
        out_trade_no=resp_json["out_trade_no"],
        resource_id=resp_json["resource_id"],
        amount=resp_json["amount"],
        active=True,
    )


# ---------------------------------------------------------------------------
# 402 出账单
# ---------------------------------------------------------------------------


class TestBillIssuance:
    def test_dedup_probe_gets_402_with_payment_needed(self, client):
        r = client.post(URL, json=_dedup_body())
        assert r.status_code == 402
        header = r.headers.get("payment-needed")
        assert header and "=" not in header
        body = r.json()
        assert body["error"] == "Payment Needed"
        assert body["op"] == "dedup"
        assert body["amount"] == "0.01"
        assert body["currency"] == "CNY"
        assert len(body["out_trade_no"]) == 32 and body["out_trade_no"].startswith("SK")
        assert len(body["resource_id"]) == 32 and body["resource_id"].startswith("D")
        assert body["pay_before"].endswith("+08:00")

    def test_bill_signature_verifies_with_public_key(self, client, keypair):
        r = client.post(URL, json=_dedup_body())
        bill = decode_payment_needed(r.headers["payment-needed"])
        protocol, method = bill["protocol"], bill["method"]
        sign_fields = {
            "amount": protocol["amount"],
            "currency": protocol["currency"],
            "goods_name": method["goods_name"],
            "out_trade_no": protocol["out_trade_no"],
            "pay_before": protocol["pay_before"],
            "resource_id": protocol["resource_id"],
            "seller_id": method["seller_id"],
            "service_id": method["service_id"],
        }
        assert method["seller_app_id"] == "9021000169635550"
        assert method["service_id"] == "api_mock_service_id"
        assert method["goods_name"] == "发票批量查重·1批（2个）"
        assert rsa2_verify(
            build_sign_content(sign_fields),
            protocol["seller_signature"],
            keypair["private"].public_key(),
        )

    def test_probe_twice_yields_new_out_trade_no(self, client):
        first = client.post(URL, json=_dedup_body()).json()
        second = client.post(URL, json=_dedup_body()).json()
        assert first["out_trade_no"] != second["out_trade_no"]

    def test_multipart_merge_probe_valid_pdf_gets_402(self, client):
        r = client.post(
            URL,
            data={"op": "merge"},
            files=[("files", ("a.pdf", b"%PDF-1.4 fake", "application/pdf"))],
        )
        assert r.status_code == 402
        assert r.json()["op"] == "merge"
        assert r.json()["resource_id"].startswith("M")

    def test_orders_created_in_db(self, client):
        r = client.post(URL, json=_dedup_body())
        store = skill_gateway._get_order_store()
        order = store.get_order(r.json()["out_trade_no"])
        assert order is not None
        assert order.status == "created"
        assert order.file_count == 2


# ---------------------------------------------------------------------------
# 验证失败路径
# ---------------------------------------------------------------------------


class TestVerifyFailures:
    def test_bad_base64_proof_gets_402_new_bill(self, client):
        first = client.post(URL, json=_dedup_body()).json()
        r = client.post(URL, json=_dedup_body(), headers={"Payment-Proof": "!!!not-base64"})
        assert r.status_code == 402
        body = r.json()
        assert body["error"] == "Payment Invalid"
        assert "支付凭证" in body["message"]
        assert body["out_trade_no"] != first["out_trade_no"]

    def test_gateway_error_maps_to_friendly_message(self, client, stub):
        stub.verify_error = AlipayGatewayError(
            "PAYMENT_PROOF_NOT_FOUND", "internal detail"
        )
        r = client.post(URL, json=_dedup_body(), headers={"Payment-Proof": _proof("T9")})
        assert r.status_code == 402
        body = r.json()
        assert body["error"] == "Payment Invalid"
        assert body["message"] == "支付凭证已过期或已使用，请重新支付"
        assert body["gateway_code"] == "PAYMENT_PROOF_NOT_FOUND"
        # 内部细节不进对客 body
        assert "internal detail" not in json.dumps(body)

    @pytest.mark.parametrize(
        "mutate",
        [
            ("amount", "9.99"),
            ("active", False),
            ("resource_id", "X" + "0" * 31),
            ("out_trade_no", "SK" + "9" * 30),
        ],
    )
    def test_business_check_failures_get_402(self, client, stub, mutate):
        probe = client.post(URL, json=_dedup_body()).json()
        result = _ok_verify_result(probe)
        field, value = mutate
        setattr(result, field, value)
        stub.verify_result = result
        r = client.post(URL, json=_dedup_body(), headers={"Payment-Proof": _proof("T1")})
        assert r.status_code == 402
        assert r.json()["error"] == "Payment Invalid"

    def test_op_mismatch_between_order_and_retry_gets_402(self, client, stub):
        probe = client.post(URL, json=_dedup_body()).json()
        stub.verify_result = _ok_verify_result(probe)
        # 重试时换了 op（文件批打 JSON 端点拿 dedup 单）→ 凭证与本单不符
        r = client.post(
            URL,
            data={"op": "rename"},
            files=[("files", ("a.xml", b"<xml/>", "application/xml"))],
            headers={"Payment-Proof": _proof("T1")},
        )
        assert r.status_code == 402


# ---------------------------------------------------------------------------
# 验证成功路径（dedup 全链路）
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_verify_success_returns_result_and_fulfills(self, client, stub):
        probe = client.post(URL, json=_dedup_body()).json()
        stub.verify_result = _ok_verify_result(probe)
        r = client.post(URL, json=_dedup_body(), headers={"Payment-Proof": _proof("T-OK-1")})
        assert r.status_code == 200
        body = r.json()
        assert body["op"] == "dedup"
        assert body["already_fulfilled"] is False
        assert body["out_trade_no"] == probe["out_trade_no"]
        assert body["trade_no"] == "T-OK-1"
        # 查重业务正确性（golden 语义）
        assert body["summary"] == {"total": 2, "ok": 2, "duplicate": 0,
                                   "suspicious": 0, "unrecognized": 0}
        assert len(body["ledgerEntries"]) == 2
        # 异步履约回执已发（TestClient 同步执行 background tasks）
        assert stub.fulfill_calls == ["T-OK-1"]
        # 订单落 fulfilled + result_cache
        store = skill_gateway._get_order_store()
        order = store.get_order(probe["out_trade_no"])
        assert order.status == "fulfilled"
        assert order.result_body()["trade_no"] == "T-OK-1"

    def test_same_trade_no_replay_verifies_then_replays(self, client, stub):
        """review② F1：回放前必须过 verify（自述 trade_no 不可信）——
        同凭证重发 → 多一次 verify 调用，但不重处理、不重复履约。"""
        probe = client.post(URL, json=_dedup_body()).json()
        stub.verify_result = _ok_verify_result(probe)
        r1 = client.post(URL, json=_dedup_body(), headers={"Payment-Proof": _proof("T-OK-1")})
        assert r1.status_code == 200
        verify_count = len(stub.verify_calls)
        # 同一凭证重发 → 先 verify（只读无副作用）再回放缓存
        r2 = client.post(URL, json=_dedup_body(), headers={"Payment-Proof": _proof("T-OK-1")})
        assert r2.status_code == 200
        body = r2.json()
        assert body["already_fulfilled"] is True
        assert body["out_trade_no"] == r1.json()["out_trade_no"]
        assert len(stub.verify_calls) == verify_count + 1  # F1：重验 +1
        assert len(stub.fulfill_calls) == 1  # 仍不重复履约

    def test_forged_trade_no_cannot_hijack_fulfilled_cache(self, client, stub):
        """review② F1 安全用例：伪造受害者 trade_no 的假凭证 → verify 拒绝 →
        402 新账单，绝不回放他人缓存（发票数据/下载链接不泄露）。"""
        probe = client.post(URL, json=_dedup_body()).json()
        stub.verify_result = _ok_verify_result(probe)
        r1 = client.post(URL, json=_dedup_body(), headers={"Payment-Proof": _proof("T-VICTIM")})
        assert r1.status_code == 200  # 受害者正常完成

        # 攻击者：只拿到 trade_no，伪造 payment_proof → 支付宝侧 verify 拒绝
        stub.verify_result = None
        stub.verify_error = AlipayGatewayError("PAYMENT_PROOF_NOT_FOUND", "forged proof")
        r2 = client.post(URL, json=_dedup_body(), headers={"Payment-Proof": _proof("T-VICTIM")})
        assert r2.status_code == 402
        body = r2.json()
        assert body["error"] == "Payment Invalid"
        assert "rows" not in body  # 受害者的查重结果未泄露
        assert "ledgerEntries" not in body

    def test_header_trade_no_mismatch_rejected_by_gateway(self, client, stub):
        """F1 补充：头声明的 trade_no 与凭证本体不一致 → 支付宝侧
        PAYMENT_PROOF_TRADE_NO_MISMATCH → 402 新账单，不触达任何缓存。"""
        stub.verify_error = AlipayGatewayError("PAYMENT_PROOF_TRADE_NO_MISMATCH", "mismatch")
        r = client.post(URL, json=_dedup_body(), headers={"Payment-Proof": _proof("T-WRONG")})
        assert r.status_code == 402
        body = r.json()
        assert body["gateway_code"] == "PAYMENT_PROOF_TRADE_NO_MISMATCH"
        assert "rows" not in body
        assert "ledgerEntries" not in body

    def test_concurrent_reentry_returns_409(self, client, stub):
        probe = client.post(URL, json=_dedup_body()).json()
        store = skill_gateway._get_order_store()
        # 另一请求已把本单占走（verified、无缓存）→ 重入 409（R2）
        assert store.claim_for_verify(probe["out_trade_no"], "T-OTHER") is True
        stub.verify_result = _ok_verify_result(probe)
        r = client.post(URL, json=_dedup_body(), headers={"Payment-Proof": _proof("T1")})
        assert r.status_code == 409
        assert r.json()["message"] == "订单正在处理中，请稍后重试"

    def test_dispatch_failure_releases_claim_for_retry(self, client, stub, monkeypatch):
        probe = client.post(URL, json=_dedup_body()).json()
        stub.verify_result = _ok_verify_result(probe)

        calls = {"n": 0}

        async def flaky_dispatch(op, payload):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("业务炸了")
            return {"op": op, "message": "ok"}

        monkeypatch.setattr(skill_gateway, "_dispatch", flaky_dispatch)
        r1 = client.post(URL, json=_dedup_body(), headers={"Payment-Proof": _proof("T1")})
        assert r1.status_code == 500  # TestClient raise_server_exceptions=False 时为 500？
        # 订单被释放回 created（不卡死 409）
        store = skill_gateway._get_order_store()
        order = store.get_order(probe["out_trade_no"])
        assert order.status == "created"
        assert order.trade_no is None
        # 同一凭证重试 → 重新验证 + 抢占 + 执行成功
        r2 = client.post(URL, json=_dedup_body(), headers={"Payment-Proof": _proof("T1")})
        assert r2.status_code == 200


# ---------------------------------------------------------------------------
# 请求解析与预检（Content-Type 双模式 / 限额 / 文件头）
# ---------------------------------------------------------------------------


class TestRequestParsing:
    def test_unsupported_content_type_400(self, client):
        r = client.post(URL, content=b"plain", headers={"Content-Type": "text/plain"})
        assert r.status_code == 400

    def test_json_requires_dedup_op(self, client):
        r = client.post(URL, json={"op": "merge", "tickets": _dedup_body()["tickets"]})
        assert r.status_code == 400

    def test_empty_tickets_400(self, client):
        r = client.post(URL, json={"op": "dedup", "tickets": []})
        assert r.status_code == 400

    def test_too_many_tickets_400(self, client):
        body = _dedup_body(n=201)
        r = client.post(URL, json=body)
        assert r.status_code == 400

    def test_oversized_json_body_413(self, client):
        big = {
            "op": "dedup",
            "tickets": [{"fileRef": "x" * 200, "number": NO1} for _ in range(200)],
            "ledger": [
                {"number": NO1, "firstName": "n" * 200, "firstDay": "2026-01-01"}
                for _ in range(40000)
            ],
        }
        r = client.post(URL, json=big)
        assert r.status_code == 413

    def test_chunked_json_body_over_limit_413(self, client):
        """review② F2：无 content-length（chunked 流式）时同样被 2MB 上限拦截。"""
        big = b'{"op":"dedup","tickets":[{"fileRef":"' + b"x" * (2 * 1024 * 1024 + 100) + b'"}]}'

        def gen():
            for i in range(0, len(big), 65536):
                yield big[i : i + 65536]

        r = client.post(URL, content=gen(), headers={"Content-Type": "application/json"})
        assert r.status_code == 413

    def test_read_bounded_json_body_caps_stream_without_content_length(self):
        """review② F2 单元级：无 content-length 头时逐 chunk 累计，超限 413
        （内存上界=limit，而非把整个流读满才兜底）。"""
        import asyncio

        from fastapi import HTTPException
        from starlette.requests import Request as StarletteRequest

        scope = {
            "type": "http", "method": "POST", "path": "/",
            "headers": [(b"content-type", b"application/json")],  # 无 content-length
            "query_string": b"",
        }
        chunks = [b"a" * 700_000, b"b" * 700_000, b"c" * 700_000, b"d" * 700_000]
        queue = iter(chunks)

        async def receive():
            try:
                chunk = next(queue)
                return {"type": "http.request", "body": chunk, "more_body": True}
            except StopIteration:
                return {"type": "http.request", "body": b"", "more_body": False}

        req = StarletteRequest(scope, receive)
        with pytest.raises(HTTPException) as excinfo:
            asyncio.run(skill_gateway._read_bounded_json_body(req, skill_gateway.MAX_JSON_BODY))
        assert excinfo.value.status_code == 413

    def test_merge_rejects_xml(self, client):
        r = client.post(
            URL,
            data={"op": "merge"},
            files=[("files", ("a.xml", b"<Invoice/>", "application/xml"))],
        )
        assert r.status_code == 400
        assert "XML" in r.json()["detail"]

    def test_extract_accepts_xml_probe_402(self, client):
        r = client.post(
            URL,
            data={"op": "extract"},
            files=[("files", ("a.xml", b"<Invoice/>", "application/xml"))],
        )
        assert r.status_code == 402
        assert r.json()["resource_id"].startswith("E")

    def test_file_count_over_50_400(self, client):
        files = [("files", (f"{i}.pdf", b"%PDF-1.4 x", "application/pdf")) for i in range(51)]
        r = client.post(URL, data={"op": "merge"}, files=files)
        assert r.status_code == 400
        assert "50" in r.json()["detail"]

    def test_magic_mismatch_400_before_payment(self, client):
        r = client.post(
            URL,
            data={"op": "extract"},
            files=[("files", ("fake.pdf", b"NOT-A-PDF", "application/pdf"))],
        )
        assert r.status_code == 400
        assert "文件头校验失败" in r.json()["detail"]

    def test_single_file_over_10mb_413_before_payment(self, client):
        big = b"%PDF-1.4 " + b"0" * (10 * 1024 * 1024 + 10)
        r = client.post(
            URL,
            data={"op": "merge"},
            files=[("files", ("big.pdf", big, "application/pdf"))],
        )
        assert r.status_code == 413

    def test_actual_total_over_limit_400(self, client, monkeypatch):
        """review② F3：实测 spool 尺寸累计超限 → 付费前 400（不信任声明值）。"""
        monkeypatch.setattr(skill_gateway, "MAX_TOTAL_SIZE", 2048)
        files = [
            ("files", (f"{i}.pdf", b"%PDF-1.4 " + b"0" * 1200, "application/pdf"))
            for i in range(2)
        ]
        r = client.post(URL, data={"op": "merge"}, files=files)
        assert r.status_code == 400
        assert "100MB" in r.json()["detail"] or "分批" in r.json()["detail"]


class TestRenameDispatch:
    def test_rename_with_proof_returns_run_batch_result(self, client, stub, monkeypatch):
        from app.models.invoice_extract import RenameBatchResponse

        probe = client.post(
            URL,
            data={"op": "rename"},
            files=[("files", ("a.pdf", b"%PDF-1.4 x", "application/pdf"))],
        ).json()
        stub.verify_result = _ok_verify_result(probe)

        captured = {}

        def fake_run_batch(files, prefix="", include_amount=False, include_type=False):
            captured["count"] = len(files)
            captured["prefix"] = prefix
            return RenameBatchResponse(
                task_id="11111111-2222-3333-4444-555555555555",
                download_url="/api/v1/invoice/rename-download/11111111-2222-3333-4444-555555555555",
                total=len(files),
                renamed=1,
                duplicates_resolved=0,
                skipped=[],
            )

        monkeypatch.setattr(skill_gateway, "rename_run_batch", fake_run_batch)
        r = client.post(
            URL,
            data={"op": "rename", "prefix": "报销_", "include_amount": "true"},
            files=[("files", ("a.pdf", b"%PDF-1.4 x", "application/pdf"))],
            headers={"Payment-Proof": _proof("T-R1")},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["op"] == "rename"
        assert body["renamed"] == 1
        assert body["already_fulfilled"] is False
        assert captured["count"] == 1
        assert captured["prefix"] == "报销_"


# ---------------------------------------------------------------------------
# 下载端点
# ---------------------------------------------------------------------------


class TestDownload:
    def test_unknown_task_404(self, client):
        r = client.get(f"{URL}/download/{uuid_mod.uuid4()}")
        assert r.status_code == 404
        assert "24 小时" in r.json()["detail"]

    def test_artifact_download_200(self, client, tmp_path):
        import os

        task_id = str(uuid_mod.uuid4())
        task_dir = tmp_path / "temp" / task_id
        task_dir.mkdir(parents=True)
        (task_dir / "发票台账_test.xlsx").write_bytes(b"xlsx-bytes")
        r = client.get(f"{URL}/download/{task_id}")
        assert r.status_code == 200
        assert r.content == b"xlsx-bytes"
        assert r.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        # 非 ASCII 文件名走 RFC 5987：filename*=utf-8''<percent-encoded>
        assert "attachment" in r.headers["content-disposition"]
        assert "%E5%8F%91%E7%A5%A8%E5%8F%B0%E8%B4%A6" in r.headers["content-disposition"]
