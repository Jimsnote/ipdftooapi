"""402 账单构造与商家签名（支付宝 AI 按量付费 / A402 协议）。

设计依据：docs/2026-10-13_invoice-toolkit-pro-p0-design.md §3 + 官方接入规范
（temp/alipay_machine_pay.md，MACHINE_PAY）。

两套签名易混点（显式区分）：
- **账单商家签名**（本模块）：8 字段按 key 字典序拼 `key=value&`，应用私钥
  RSA2（SHA256 + PKCS#1 v1.5）→ **标准 Base64**；
- **网关请求签名**（alipay_client.py）：开放平台公共参数签名，同为 RSA2
  但拼串内容不同。
"""
import base64
import json
import secrets
import time
import uuid as _uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

# 中国标准时间（账单时间戳与网关 timestamp 均以此输出，与官方示例一致）
CST = timezone(timedelta(hours=8))

SANDBOX_GATEWAY = "https://openapi-sandbox.dl.alipaydev.com/gateway.do"
PROD_GATEWAY = "https://openapi.alipay.com/gateway.do"

CURRENCY = "CNY"
SIGN_TYPE = "RSA2"
SELLER_UNIQUE_ID_KEY = "seller_id"

# 参与商家签名的 8 字段（官方规范原文；goods_name/pay_before 含中文与 + : 原样进串）
SIGN_FIELDS = (
    "amount",
    "currency",
    "goods_name",
    "out_trade_no",
    "pay_before",
    "resource_id",
    "seller_id",
    "service_id",
)

GOODS_NAME_TEMPLATES = {
    "merge": "发票合并打印·1批（{n}张）",
    "extract": "发票台账提取·1批（{n}个）",
    "rename": "发票批量重命名·1批（{n}个）",
    "dedup": "发票批量查重·1批（{n}个）",
}

# resource_id 的 op 前缀（1 字符 + uuid4 hex 31 = 32 位，verify 响应该字段限 32）
RESOURCE_PREFIX = {"merge": "M", "extract": "E", "rename": "R", "dedup": "D"}


class PaymentProofError(ValueError):
    """Payment-Proof 头解析失败（缺字段/坏 Base64/坏 JSON）——网关层转 402 而非 500。"""


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillPayConfig:
    """skill_gateway 所需的全部支付配置（从 Settings 构建，便于测试注入）。"""

    app_id: str
    private_key: str  # 应用私钥（PKCS#8/PKCS#1 裸 base64 或完整 PEM 均可）
    alipay_public_key: str = ""  # 支付宝公钥（验网关响应签名；空则跳过验签）
    seller_id: str = ""
    seller_name: str = "iPDFToo"
    service_id: str = ""
    amount: str = "0.01"
    pay_before_minutes: int = 15
    sandbox: bool = False

    @property
    def gateway_url(self) -> str:
        return SANDBOX_GATEWAY if self.sandbox else PROD_GATEWAY


def skillpay_config_from_settings(settings) -> SkillPayConfig:
    return SkillPayConfig(
        app_id=settings.SKILLPAY_APP_ID,
        private_key=settings.SKILLPAY_PRIVATE_KEY,
        alipay_public_key=settings.SKILLPAY_ALIPAY_PUBLIC_KEY,
        seller_id=settings.SKILLPAY_SELLER_ID,
        seller_name=settings.SKILLPAY_SELLER_NAME,
        service_id=settings.SKILLPAY_SERVICE_ID,
        amount=settings.SKILLPAY_AMOUNT,
        pay_before_minutes=settings.SKILLPAY_PAY_BEFORE_MINUTES,
        sandbox=settings.SKILLPAY_SANDBOX,
    )


# ---------------------------------------------------------------------------
# 密钥加载（缓存：同一份密钥材料在进程内只解析一次）
# ---------------------------------------------------------------------------


@lru_cache(maxsize=8)
def _load_private_key_cached(normalized: str) -> rsa.RSAPrivateKey:
    if "-----BEGIN" in normalized:
        key = serialization.load_pem_private_key(normalized.encode("utf-8"), password=None)
    else:
        last_error: Optional[ValueError] = None
        for header in ("PRIVATE KEY", "RSA PRIVATE KEY"):  # 先 PKCS#8 后 PKCS#1
            pem = f"-----BEGIN {header}-----\n{normalized}\n-----END {header}-----".encode()
            try:
                key = serialization.load_pem_private_key(pem, password=None)
                break
            except ValueError as e:  # noqa: PERF203
                last_error = e
        else:
            raise ValueError(
                f"应用私钥格式无法解析（应为 PKCS#8/PKCS#1 裸 base64 或 PEM）：{last_error}"
            )
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("应用私钥不是 RSA 私钥")
    return key


@lru_cache(maxsize=8)
def _load_public_key_cached(normalized: str) -> rsa.RSAPublicKey:
    if "-----BEGIN" in normalized:
        key = serialization.load_pem_public_key(normalized.encode("utf-8"))
    else:
        pem = f"-----BEGIN PUBLIC KEY-----\n{normalized}\n-----END PUBLIC KEY-----".encode()
        key = serialization.load_pem_public_key(pem)
    if not isinstance(key, rsa.RSAPublicKey):
        raise ValueError("公钥不是 RSA 公钥")
    return key


def load_private_key(material: str) -> rsa.RSAPrivateKey:
    normalized = " ".join((material or "").split())
    if not normalized:
        raise ValueError("SKILLPAY_PRIVATE_KEY 未配置")
    return _load_private_key_cached(normalized)


def load_public_key(material: str) -> rsa.RSAPublicKey:
    normalized = " ".join((material or "").split())
    if not normalized:
        raise ValueError("公钥内容为空")
    return _load_public_key_cached(normalized)


# ---------------------------------------------------------------------------
# 号单生成
# ---------------------------------------------------------------------------


def generate_out_trade_no() -> str:
    """SK + Unix 秒级时间戳(10) + 随机 hex(20) = 32 位全局唯一（DB 唯一约束兜底）。"""
    return "SK" + str(int(time.time())) + secrets.token_hex(10)


def generate_resource_id(op: str) -> str:
    """op 前缀 1 字符 + uuid4 hex 31 = 32 位（请求级，存订单表，防资源串号）。"""
    try:
        prefix = RESOURCE_PREFIX[op]
    except KeyError:
        raise ValueError(f"未知 op：{op}") from None
    return prefix + _uuid.uuid4().hex[:31]


def goods_name_for(op: str, file_count: int) -> str:
    return GOODS_NAME_TEMPLATES[op].format(n=file_count)


# ---------------------------------------------------------------------------
# 签名
# ---------------------------------------------------------------------------


def build_sign_content(fields: dict) -> str:
    """官方规范：8 字段按 key 字典序拼 `key=value&`（无 URL 编码、无空格）。

    官方示例（字节级锚点，见 tests/test_skillpay_payment.py）：
    amount=0.01&currency=CNY&goods_name=测试商品&out_trade_no=ORDER_…&pay_before=2026-03-25T12:00:00+08:00&resource_id=RES_…&seller_id=2088xxxxxxxx&service_id=xxxx_12344
    """
    missing = [k for k in SIGN_FIELDS if k not in fields]
    if missing:
        raise ValueError(f"签名字段缺失：{missing}")
    return "&".join(f"{k}={fields[k]}" for k in sorted(fields))


def rsa2_sign(content: str, private_key: rsa.RSAPrivateKey) -> str:
    """SHA256 摘要 + RSA PKCS#1 v1.5 签名 → 标准 Base64（非 URL-safe、不带换行）。"""
    signature = private_key.sign(
        content.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256()
    )
    return base64.b64encode(signature).decode("ascii")


def rsa2_verify(content: str, signature_b64: str, public_key: rsa.RSAPublicKey) -> bool:
    try:
        signature = base64.b64decode(signature_b64)
    except (ValueError, TypeError):
        return False
    try:
        public_key.verify(signature, content.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:  # noqa: BLE001 - InvalidSignature 等一律 False
        return False


# ---------------------------------------------------------------------------
# 账单构造与编码
# ---------------------------------------------------------------------------


def build_bill(
    cfg: SkillPayConfig,
    *,
    op: str,
    file_count: int,
    out_trade_no: str,
    resource_id: str,
    pay_before_minutes: Optional[int] = None,
) -> dict:
    """构造双层账单 JSON（protocol + method），含商家签名。"""
    private_key = load_private_key(cfg.private_key)
    pay_before = (
        datetime.now(CST).replace(microsecond=0)
        + timedelta(minutes=pay_before_minutes if pay_before_minutes is not None else cfg.pay_before_minutes)
    ).isoformat()

    sign_fields = {
        "amount": cfg.amount,
        "currency": CURRENCY,
        "goods_name": goods_name_for(op, file_count),
        "out_trade_no": out_trade_no,
        "pay_before": pay_before,
        "resource_id": resource_id,
        "seller_id": cfg.seller_id,
        "service_id": cfg.service_id,
    }
    seller_signature = rsa2_sign(build_sign_content(sign_fields), private_key)

    return {
        "protocol": {
            "out_trade_no": out_trade_no,
            "amount": cfg.amount,
            "currency": CURRENCY,
            "resource_id": resource_id,
            "pay_before": pay_before,
            "seller_signature": seller_signature,
            "seller_sign_type": SIGN_TYPE,
            "seller_unique_id": cfg.seller_id,
        },
        "method": {
            "seller_name": cfg.seller_name,
            "seller_id": cfg.seller_id,
            "seller_app_id": cfg.app_id,
            "goods_name": sign_fields["goods_name"],
            "seller_unique_id_key": SELLER_UNIQUE_ID_KEY,
            "service_id": cfg.service_id,
        },
    }


def encode_payment_needed(bill: dict) -> str:
    """账单 JSON → UTF-8 → Base64URL（去 padding），进 Payment-Needed 响应头。"""
    payload = json.dumps(bill, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def decode_payment_needed(encoded: str) -> dict:
    """Payment-Needed 头解码（测试与排障用；宽容缺失 padding）。"""
    pad = "=" * (-len(encoded) % 4)
    return json.loads(base64.urlsafe_b64decode(encoded + pad).decode("utf-8"))


# ---------------------------------------------------------------------------
# Payment-Proof 解析
# ---------------------------------------------------------------------------


def decode_payment_proof(header: str) -> tuple:
    """解析买家 Payment-Proof 头 → (payment_proof, trade_no, client_session|None)。

    头 = Base64（标准或 URL-safe 均兼容）的 JSON：
    {"protocol": {"payment_proof": "…", "trade_no": "…"},
     "method":   {"client_session": "…"}}
    一切解析失败抛 PaymentProofError（网关层转 402 新账单，绝不 500）。
    """
    raw = (header or "").strip()
    if not raw:
        raise PaymentProofError("支付凭证为空")
    pad = "=" * (-len(raw) % 4)
    try:
        data = base64.urlsafe_b64decode(raw + pad)
    except Exception as e:  # noqa: BLE001
        raise PaymentProofError("支付凭证不是合法 Base64") from e
    try:
        obj = json.loads(data.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise PaymentProofError("支付凭证不是合法 JSON") from e
    if not isinstance(obj, dict):
        raise PaymentProofError("支付凭证结构不正确")
    protocol = obj.get("protocol") if isinstance(obj.get("protocol"), dict) else {}
    method = obj.get("method") if isinstance(obj.get("method"), dict) else {}
    payment_proof = protocol.get("payment_proof")
    trade_no = protocol.get("trade_no")
    if not payment_proof or not trade_no:
        raise PaymentProofError("支付凭证缺少 payment_proof/trade_no")
    client_session = method.get("client_session")
    return str(payment_proof), str(trade_no), (str(client_session) if client_session else None)
