"""skillpay payment 模块测试（P0 设计 §9 单测-签名/单测-账单）。

签名串构造以官方接入规范示例做**字节级断言**（temp/alipay_machine_pay.md）
——字典序、`+08:00` 冒号、中文原样、无 URL 编码。
"""
import base64
import json
import re
from datetime import datetime, timedelta

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from app.services.skillpay.alipay_client import (
    AlipayClient,
    AlipayGatewayError,
)
from app.services.skillpay.payment import (
    CURRENCY,
    PaymentProofError,
    SkillPayConfig,
    build_bill,
    build_sign_content,
    decode_payment_needed,
    decode_payment_proof,
    encode_payment_needed,
    generate_out_trade_no,
    generate_resource_id,
    goods_name_for,
    load_private_key,
    load_public_key,
    rsa2_sign,
    rsa2_verify,
)

# 官方文档示例串（字节级锚点）
OFFICIAL_SIGN_STRING = (
    "amount=0.01&currency=CNY&goods_name=测试商品"
    "&out_trade_no=ORDER_1739836600000_abc123"
    "&pay_before=2026-03-25T12:00:00+08:00"
    "&resource_id=RES_1739836600000_abc123"
    "&seller_id=2088xxxxxxxx&service_id=xxxx_12344"
)

OFFICIAL_FIELDS = {
    "amount": "0.01",
    "currency": "CNY",
    "goods_name": "测试商品",
    "out_trade_no": "ORDER_1739836600000_abc123",
    "pay_before": "2026-03-25T12:00:00+08:00",
    "resource_id": "RES_1739836600000_abc123",
    "seller_id": "2088xxxxxxxx",
    "service_id": "xxxx_12344",
}


@pytest.fixture(scope="module")
def keypair():
    """测试 RSA 密钥对（PKCS#8 PEM / PKCS#1 PEM / 裸 base64 三形态）。"""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pkcs8_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    pkcs1_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ).decode()
    public_pem = private.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return {
        "private": private,
        "pkcs8_pem": pkcs8_pem,
        "pkcs1_pem": pkcs1_pem,
        "pkcs8_b64": "".join(pkcs8_pem.strip().splitlines()[1:-1]),
        "pkcs1_b64": "".join(pkcs1_pem.strip().splitlines()[1:-1]),
        "public_pem": public_pem,
        "public_b64": "".join(public_pem.strip().splitlines()[1:-1]),
    }


def _cfg(keypair, **over):
    base = dict(
        app_id="2021000000000001",
        private_key=keypair["pkcs8_b64"],
        alipay_public_key=keypair["public_b64"],
        seller_id="2088721115842304",
        seller_name="iPDFToo",
        service_id="api_mock_service_id",
        amount="0.01",
        pay_before_minutes=15,
        sandbox=True,
    )
    base.update(over)
    return SkillPayConfig(**base)


# ---------------------------------------------------------------------------
# 签名串构造（官方示例字节级断言）
# ---------------------------------------------------------------------------


class TestBuildSignContent:
    def test_official_example_byte_level(self):
        assert build_sign_content(OFFICIAL_FIELDS) == OFFICIAL_SIGN_STRING

    def test_dict_order_regardless_of_input_order(self):
        shuffled = {k: OFFICIAL_FIELDS[k] for k in reversed(list(OFFICIAL_FIELDS))}
        assert build_sign_content(shuffled) == OFFICIAL_SIGN_STRING

    def test_missing_field_raises(self):
        fields = dict(OFFICIAL_FIELDS)
        del fields["amount"]
        with pytest.raises(ValueError, match="amount"):
            build_sign_content(fields)


class TestRsa2Sign:
    def test_sign_then_verify_roundtrip(self, keypair):
        signature = rsa2_sign(OFFICIAL_SIGN_STRING, keypair["private"])
        # 标准 Base64（可含 + /，不含 - _），可被严格模式解码
        base64.b64decode(signature, validate=True)
        assert rsa2_verify(OFFICIAL_SIGN_STRING, signature, keypair["private"].public_key())

    def test_tampered_content_fails(self, keypair):
        signature = rsa2_sign(OFFICIAL_SIGN_STRING, keypair["private"])
        assert not rsa2_verify(OFFICIAL_SIGN_STRING + "x", signature, keypair["private"].public_key())

    def test_bad_signature_b64_returns_false(self, keypair):
        assert not rsa2_verify(OFFICIAL_SIGN_STRING, "not-base64!!", keypair["private"].public_key())


# ---------------------------------------------------------------------------
# 密钥加载（PKCS#8 / PKCS#1 / PEM 三形态，T2 沙箱即 PKCS#8 裸 base64）
# ---------------------------------------------------------------------------


class TestKeyLoading:
    def test_all_private_key_formats_load_to_same_key(self, keypair):
        for material in (keypair["pkcs8_pem"], keypair["pkcs1_pem"],
                         keypair["pkcs8_b64"], keypair["pkcs1_b64"]):
            loaded = load_private_key(material)
            assert (
                loaded.public_key().public_numbers()
                == keypair["private"].public_key().public_numbers()
            )

    def test_public_key_pem_and_bare_b64(self, keypair):
        for material in (keypair["public_pem"], keypair["public_b64"]):
            assert load_public_key(material) is not None

    def test_empty_private_key_raises(self):
        with pytest.raises(ValueError, match="未配置"):
            load_private_key("")

    def test_garbage_private_key_raises(self):
        with pytest.raises(ValueError, match="无法解析"):
            load_private_key("not-a-key-material")


# ---------------------------------------------------------------------------
# 号单与账单
# ---------------------------------------------------------------------------


class TestIdGeneration:
    def test_out_trade_no_32_chars_unique(self):
        a, b = generate_out_trade_no(), generate_out_trade_no()
        assert len(a) == 32 and a.startswith("SK")
        assert a != b
        assert re.match(r"^SK\d{10}[0-9a-f]{20}$", a)

    def test_resource_id_32_chars_with_op_prefix(self):
        for op, prefix in (("merge", "M"), ("extract", "E"), ("rename", "R"), ("dedup", "D")):
            rid = generate_resource_id(op)
            assert len(rid) == 32 and rid.startswith(prefix)
        with pytest.raises(ValueError):
            generate_resource_id("unknown")

    def test_goods_name_templates(self):
        assert goods_name_for("merge", 12) == "发票合并打印·1批（12张）"
        assert goods_name_for("dedup", 3) == "发票批量查重·1批（3个）"


class TestBuildBill:
    def test_bill_structure_and_signature(self, keypair):
        cfg = _cfg(keypair)
        bill = build_bill(
            cfg, op="merge", file_count=12,
            out_trade_no="SK1739836600" + "a" * 20,
            resource_id="M" + "b" * 31,
        )
        protocol, method = bill["protocol"], bill["method"]
        # protocol 8 字段
        assert set(protocol) == {
            "out_trade_no", "amount", "currency", "resource_id", "pay_before",
            "seller_signature", "seller_sign_type", "seller_unique_id",
        }
        assert protocol["amount"] == cfg.amount
        assert protocol["currency"] == CURRENCY
        assert protocol["seller_sign_type"] == "RSA2"
        assert protocol["seller_unique_id"] == cfg.seller_id
        # method 6 字段
        assert set(method) == {
            "seller_name", "seller_id", "seller_app_id", "goods_name",
            "seller_unique_id_key", "service_id",
        }
        assert method["seller_app_id"] == cfg.app_id
        assert method["goods_name"] == "发票合并打印·1批（12张）"

        # 签名可用公钥验回（8 字段串 = 官方规范拼法）
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
        assert rsa2_verify(
            build_sign_content(sign_fields),
            protocol["seller_signature"],
            keypair["private"].public_key(),
        )

    def test_pay_before_15min_iso8601_cst(self, keypair):
        before = datetime.now(keypair and __import__(
            "app.services.skillpay.payment", fromlist=["CST"]
        ).CST)
        bill = build_bill(
            _cfg(keypair), op="dedup", file_count=1,
            out_trade_no=generate_out_trade_no(),
            resource_id=generate_resource_id("dedup"),
        )
        pay_before = bill["protocol"]["pay_before"]
        assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$", pay_before)
        parsed = datetime.fromisoformat(pay_before)
        assert timedelta(minutes=14) <= parsed - before <= timedelta(minutes=16)

    def test_custom_pay_before_minutes(self, keypair):
        bill = build_bill(
            _cfg(keypair), op="merge", file_count=1,
            out_trade_no=generate_out_trade_no(),
            resource_id=generate_resource_id("merge"),
            pay_before_minutes=30,
        )
        parsed = datetime.fromisoformat(bill["protocol"]["pay_before"])
        from app.services.skillpay.payment import CST

        assert parsed - datetime.now(CST) > timedelta(minutes=28)


class TestPaymentNeededCodec:
    def test_roundtrip_no_padding(self, keypair):
        bill = build_bill(
            _cfg(keypair), op="extract", file_count=2,
            out_trade_no=generate_out_trade_no(),
            resource_id=generate_resource_id("extract"),
        )
        encoded = encode_payment_needed(bill)
        assert "=" not in encoded
        assert decode_payment_needed(encoded) == bill

    def test_chinese_survives_utf8_base64url(self):
        payload = {"msg": "发票合并打印·1批（12张）"}
        encoded = encode_payment_needed(payload)
        assert decode_payment_needed(encoded) == payload


# ---------------------------------------------------------------------------
# Payment-Proof 解析（容错：坏输入一律 PaymentProofError，绝不 500）
# ---------------------------------------------------------------------------


def _proof_b64(protocol: dict, method: dict, urlsafe: bool = False) -> str:
    raw = json.dumps({"protocol": protocol, "method": method}).encode("utf-8")
    encoded = (
        base64.urlsafe_b64encode(raw) if urlsafe else base64.b64encode(raw)
    )
    return encoded.decode("ascii").rstrip("=")


class TestDecodePaymentProof:
    def test_valid_standard_base64(self):
        pp, tn, cs = decode_payment_proof(
            _proof_b64(
                {"payment_proof": "pp64chars", "trade_no": "2026040900828111"},
                {"client_session": "cs1"},
            )
        )
        assert (pp, tn, cs) == ("pp64chars", "2026040900828111", "cs1")

    def test_valid_urlsafe_base64_without_padding(self):
        pp, tn, cs = decode_payment_proof(
            _proof_b64(
                {"payment_proof": "pp", "trade_no": "T"},
                {"client_session": "cs"},
                urlsafe=True,
            )
        )
        assert (pp, tn, cs) == ("pp", "T", "cs")

    def test_client_session_optional(self):
        pp, tn, cs = decode_payment_proof(
            _proof_b64({"payment_proof": "pp", "trade_no": "T"}, {})
        )
        assert cs is None

    @pytest.mark.parametrize("bad", ["", "   ", "!!!not-base64###"])
    def test_bad_base64_raises(self, bad):
        with pytest.raises(PaymentProofError):
            decode_payment_proof(bad)

    def test_bad_json_raises(self):
        with pytest.raises(PaymentProofError):
            decode_payment_proof(base64.b64encode(b"not json").decode())

    def test_missing_trade_no_raises(self):
        with pytest.raises(PaymentProofError):
            decode_payment_proof(
                _proof_b64({"payment_proof": "pp"}, {"client_session": "cs"})
            )

    def test_missing_payment_proof_raises(self):
        with pytest.raises(PaymentProofError):
            decode_payment_proof(_proof_b64({"trade_no": "T"}, {}))

    def test_non_dict_payload_raises(self):
        raw = base64.b64encode(b"[1,2,3]").decode()
        with pytest.raises(PaymentProofError):
            decode_payment_proof(raw)


# ---------------------------------------------------------------------------
# 网关响应解释（T2 沙箱实证驱动的行为，alipay_client._interpret_response）
# ---------------------------------------------------------------------------

_VERIFY_METHOD = "alipay.aipay.agent.payment.verify"
_VERIFY_NODE_KEY = "alipay_aipay_agent_payment_verify_response"


class TestInterpretResponse:
    """T2 实证（2026-10-13）：
    1. 业务失败（code!=10000，如 40004/SYSTEM_ERROR）藏在成功节点内；
    2. 沙箱响应签名无法用交付公钥验证 → sandbox=True 跳过验签；
    3. 生产模式严格验签（原始子串 + 支付宝公钥，fail-closed）。
    """

    def _client(self, keypair, *, sandbox=True, with_alipay_key=False):
        cfg = _cfg(
            keypair,
            sandbox=sandbox,
            alipay_public_key=keypair["public_b64"] if with_alipay_key else "",
        )
        return AlipayClient(cfg)

    @staticmethod
    def _signed_text(keypair, node: dict, *, tamper_sign=False):
        raw_node = json.dumps(node, separators=(",", ":"), ensure_ascii=False)
        sign = rsa2_sign(raw_node, keypair["private"])
        if tamper_sign:
            sign = ("A" if sign[0] != "A" else "B") + sign[1:]
        return '{"%s":%s,"sign":"%s"}' % (_VERIFY_NODE_KEY, raw_node, sign)

    def test_business_failure_inside_success_node_raises(self, keypair):
        """T2 实测响应：40004/SYSTEM_ERROR 在成功节点内 → 必须抛出业务错误。"""
        client = self._client(keypair, sandbox=True)
        text = self._signed_text(
            keypair,
            {"msg": "Business Failed", "code": "40004", "sub_msg": "系统繁忙", "sub_code": "SYSTEM_ERROR"},
        )
        with pytest.raises(AlipayGatewayError) as ei:
            client._interpret_response(text, _VERIFY_METHOD)
        assert ei.value.sub_code == "SYSTEM_ERROR"
        assert ei.value.user_message == "支付宝服务繁忙，请重试（幂等安全）"

    def test_success_node_code_10000_passes(self, keypair):
        client = self._client(keypair, sandbox=True)
        node = {
            "code": "10000", "msg": "Success", "trade_no": "T1",
            "out_trade_no": "SK" + "1" * 30, "resource_id": "D" + "2" * 31,
            "amount": "0.01", "active": True,
        }
        text = self._signed_text(keypair, node)
        assert client._interpret_response(text, _VERIFY_METHOD) == node

    def test_unverifiable_sign_warns_but_passes(self, keypair):
        """T2/T3 双环境实证：交付公钥验不出 aipay 网关响应签名 → 尽力而为
        （失败记 WARNING 不挡业务——官方规范与 Python demo 均不做响应验签）。"""
        client = self._client(keypair, sandbox=True, with_alipay_key=True)
        node = {"code": "10000", "trade_no": "T1"}
        text = self._signed_text(keypair, node, tamper_sign=True)  # 故意坏签名
        assert client._interpret_response(text, _VERIFY_METHOD) == node  # 不再阻断

    def test_production_verifies_sign_over_raw_substring(self, keypair):
        client = self._client(keypair, sandbox=False, with_alipay_key=True)
        node = {"code": "10000", "trade_no": "T1", "active": True}
        text = self._signed_text(keypair, node)
        assert client._interpret_response(text, _VERIFY_METHOD) == node

    def test_production_bad_sign_warns_but_passes(self, keypair):
        """T3 生产实证决策：验签失败降级 WARNING 不抛错——严格 fail-closed
        会挡住已付款买家（生产冒烟实测 SIGN_VERIFY_FAILED 阻断交付路径）。"""
        client = self._client(keypair, sandbox=False, with_alipay_key=True)
        node = {"code": "10000", "trade_no": "T1"}
        text = self._signed_text(keypair, node, tamper_sign=True)
        assert client._interpret_response(text, _VERIFY_METHOD) == node  # 尽力而为

    def test_verify_payment_camel_case_fields(self, keypair):
        """官方 demo 双兼容：网关响应字段可能是 camelCase（tradeNo/outTradeNo/
        resourceId）——缺该兼容会解析出空值 → 五项校验失败 → 挡住已付款买家。"""
        client = self._client(keypair, sandbox=True)

        def fake_call(method, biz_content, timeout=10.0):
            return {
                "code": "10000", "msg": "Success",
                "tradeNo": "T-CAMEL-1", "outTradeNo": "SK" + "1" * 30,
                "resourceId": "D" + "2" * 31, "amount": "0.01", "active": True,
            }

        client._call = fake_call
        result = client.verify_payment("pp", "T-CAMEL-1", None)
        assert result.trade_no == "T-CAMEL-1"
        assert result.out_trade_no == "SK" + "1" * 30
        assert result.resource_id == "D" + "2" * 31
        assert result.amount == "0.01"
        assert result.active is True

    def test_error_response_raises_with_sub_code(self, keypair):
        client = self._client(keypair, sandbox=True)
        text = json.dumps(
            {"error_response": {"code": "40002", "sub_code": "PAYMENT_PROOF_NOT_FOUND",
                                 "sub_msg": "凭证不存在"}}
        )
        with pytest.raises(AlipayGatewayError) as ei:
            client._interpret_response(text, _VERIFY_METHOD)
        assert ei.value.sub_code == "PAYMENT_PROOF_NOT_FOUND"

    def test_not_json_raises(self, keypair):
        client = self._client(keypair, sandbox=True)
        with pytest.raises(AlipayGatewayError) as ei:
            client._interpret_response("<html>bad gateway</html>", _VERIFY_METHOD)
        assert ei.value.sub_code == "RESPONSE_NOT_JSON"

    def test_missing_node_raises(self, keypair):
        client = self._client(keypair, sandbox=True)
        with pytest.raises(AlipayGatewayError) as ei:
            client._interpret_response(json.dumps({"other_response": {}}), _VERIFY_METHOD)
        assert ei.value.sub_code == "RESPONSE_NODE_MISSING"
