"""支付宝开放平台网关客户端（P0 设计 §5）：验凭证 + 履约回执。

设计依据：docs/2026-10-13_invoice-toolkit-pro-p0-design.md §5 + 官方 API 文档
（temp/alipay_verify_api.md / temp/alipay_fulfill_api.md）。

网关请求签名（与账单商家签名是两套，勿混）：
公共参数（app_id/method/charset/sign_type/timestamp/version/format/biz_content）
除 sign 外按 key 字典序拼 `key=value&` → 应用私钥 RSA2 → POST form。
响应用支付宝公钥验签（防伪造响应）；验签串取响应节点的**原始子串**
（Java SDK 同款做法——重排序/重序列化的 JSON 与官方签名不匹配）。
"""
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import httpx

from app.core.logger import get_logger
from app.services.skillpay.payment import (
    CST,
    SkillPayConfig,
    load_private_key,
    load_public_key,
    rsa2_sign,
    rsa2_verify,
)

logger = get_logger(__name__)

# 错误码 → 人话（进 402 body 的 message + 日志；P0 §5.4）
ERROR_USER_MESSAGES = {
    "PAYMENT_PROOF_NOT_FOUND": "支付凭证已过期或已使用，请重新支付",
    "PAYMENT_PROOF_STATUS_INVALID": "支付凭证已过期或已使用，请重新支付",
    "PAYMENT_PROOF_AGENT_MISMATCH": "支付凭证与商户/买家不匹配（安全拦截）",
    "PAYMENT_PROOF_ID_MISMATCH": "支付凭证与商户/买家不匹配（安全拦截）",
    "PAYMENT_PROOF_BUYER_MISMATCH": "支付凭证与商户/买家不匹配（安全拦截）",
    "PAYMENT_PROOF_TRADE_NO_MISMATCH": "支付凭证与交易号不匹配，请重新支付",
    "TRADE_STATUS_UNPAID": "支付尚未完成，请稍后重试",
    "TRADE_STATUS_CLOSED": "交易已关闭或不存在，请重新发起",
    "TRADE_NOT_FOUND": "交易已关闭或不存在，请重新发起",
    "CLIENT_SESSION_IS_EMPTY": "买家支付组件需升级到最新版后重试",
    "NETWORK_ERROR": "支付平台响应超时，请稍后重试（不会重复扣款）",
    "SYSTEM_ERROR": "支付宝服务繁忙，请重试（幂等安全）",
}
DEFAULT_USER_MESSAGE = "支付验证失败，请重新支付"


def map_error_message(sub_code: str) -> str:
    return ERROR_USER_MESSAGES.get(sub_code, DEFAULT_USER_MESSAGE)


class AlipayGatewayError(Exception):
    """网关调用失败（error_response / 网络异常 / 响应验签失败）。

    user_message 可直接进 402 body；sub_code/内部信息仅供日志。
    """

    def __init__(self, sub_code: str, internal: str = "", user_message: Optional[str] = None):
        self.sub_code = sub_code
        self.internal = internal
        self.user_message = user_message or map_error_message(sub_code)
        super().__init__(f"[{sub_code}] {internal}")


@dataclass
class VerifyResult:
    """alipay.aipay.agent.payment.verify 响应（五项业务校验的输入）。"""

    trade_no: str
    out_trade_no: str
    resource_id: str
    amount: str
    active: bool


class AlipayClient:
    def __init__(self, cfg: SkillPayConfig):
        if not cfg.app_id:
            raise ValueError("SKILLPAY_APP_ID 未配置")
        self.cfg = cfg
        self.gateway = cfg.gateway_url
        self._private_key = load_private_key(cfg.private_key)
        self._alipay_public_key = (
            load_public_key(cfg.alipay_public_key) if cfg.alipay_public_key else None
        )

    # ------------------------------------------------------------------
    # 网关调用
    # ------------------------------------------------------------------

    def _call(self, method: str, biz_content: dict, timeout: float = 10.0) -> dict:
        params = {
            "app_id": self.cfg.app_id,
            "method": method,
            "charset": "utf-8",
            "sign_type": "RSA2",
            "timestamp": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S"),
            "version": "1.0",
            "format": "JSON",
            "biz_content": json.dumps(biz_content, ensure_ascii=False, separators=(",", ":")),
        }
        sign_content = "&".join(f"{k}={params[k]}" for k in sorted(params))
        params["sign"] = rsa2_sign(sign_content, self._private_key)

        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(self.gateway, data=params)
        except httpx.HTTPError as e:
            raise AlipayGatewayError("NETWORK_ERROR", repr(e)) from e
        if resp.status_code != 200:
            raise AlipayGatewayError("HTTP_%d" % resp.status_code, resp.text[:500])
        return self._interpret_response(resp.text, method)

    def _interpret_response(self, raw_text: str, method: str) -> dict:
        """解释网关响应原文（独立方法便于单测）。

        T2/T3 实证（2026-10-13，沙箱+生产双环境）：
        1. 业务失败以 `code != 10000` 藏在成功节点内（如 40004/TRADE_NOT_FOUND），
           顶层 error_response 只承载协议级错误——必须显式检查业务码；
        2. aipay 系列网关的响应签名无法用交付公钥验出（官方规范与 Python demo
           均不做响应验签）——验签降级为**尽力而为**（失败记 WARNING 不挡业务），
           决策链见下方验签段注释；
        3. 响应字段名存在 snake_case/camelCase 两种风格（官方 demo 双兼容）——
           verify_payment 解析侧做双兼容。
        """
        try:
            payload = json.loads(raw_text)
        except ValueError as e:
            raise AlipayGatewayError("RESPONSE_NOT_JSON", raw_text[:500]) from e

        if "error_response" in payload:
            err = payload["error_response"] or {}
            sub_code = str(err.get("sub_code") or err.get("code") or "SYSTEM_ERROR")
            internal = str(err.get("sub_msg") or err.get("msg") or "")
            raise AlipayGatewayError(sub_code, internal)

        node_key = method.replace(".", "_") + "_response"
        node = payload.get(node_key)
        if not isinstance(node, dict):
            raise AlipayGatewayError("RESPONSE_NODE_MISSING", f"缺少 {node_key} 节点")

        # 响应验签（尽力而为，T3 生产实证决策 2026-10-13）：
        # 沙箱+生产双环境穷举（各 6 候选串 × 两把交付公钥）均验不出 aipay 系列
        # 网关的响应签名；官方接入规范"三件事"与官方 Python demo（A2MPaymentDemo）
        # 均不含响应验签。故降级为尽力而为：验不过记 WARNING 不挡业务——伪造
        # 响应需先攻破出站 TLS，且须匹配我方订单的 out_trade_no/resource_id/
        # amount 三重绑定（F1 修复后回放同样过 verify）。
        sign = payload.get("sign")
        if not sign:
            logger.warning("alipay gateway response without sign: method=%s", method)
        elif self._alipay_public_key is not None:
            raw_node = self._extract_node_raw(raw_text, node_key)
            if not rsa2_verify(raw_node, str(sign), self._alipay_public_key):
                logger.warning(
                    "alipay response sign verification FAILED (best-effort): method=%s "
                    "node_code=%s——不阻断业务（见 _interpret_response docstring 决策链）",
                    method, node.get("code"),
                )
        else:
            logger.warning("alipay response sign present but SKILLPAY_ALIPAY_PUBLIC_KEY 未配置，跳过验签")

        # 业务码检查（T2 实证：业务失败藏在成功节点内，code != 10000）
        code = str(node.get("code") or "")
        if code and code != "10000":
            sub_code = str(node.get("sub_code") or code)
            internal = str(node.get("sub_msg") or node.get("msg") or "")
            raise AlipayGatewayError(sub_code, internal)
        return node

    @staticmethod
    def _extract_node_raw(text: str, node_key: str) -> str:
        """从响应原文提取 x_response 节点的原始 JSON 子串（字符串感知的括号配对）。"""
        marker = f'"{node_key}"'
        idx = text.find(marker)
        if idx < 0:
            raise AlipayGatewayError("SIGN_VERIFY_FAILED", f"无法定位 {node_key} 原文")
        start = text.find("{", idx + len(marker))
        if start < 0:
            raise AlipayGatewayError("SIGN_VERIFY_FAILED", "响应节点原文格式异常")
        depth, in_str, esc = 0, False, False
        for j in range(start, len(text)):
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : j + 1]
        raise AlipayGatewayError("SIGN_VERIFY_FAILED", "响应节点原文括号不闭合")

    # ------------------------------------------------------------------
    # 两个业务接口
    # ------------------------------------------------------------------

    def verify_payment(
        self, payment_proof: str, trade_no: str, client_session: Optional[str]
    ) -> VerifyResult:
        """验凭证：入参全部来自买家 Payment-Proof 头。

        响应字段 snake_case/camelCase 双兼容（官方 A2MPaymentDemo 同款防御：
        网关返回风格不确定，缺一兼容会解析出空值 → 挡住已付款买家）。
        超时 20s：生产实测（2026-10-13 验收）10s 偶发 ReadTimeout →
        verify 失败 → 买家侧 402 惊吓轮询；验凭证是只读操作，放宽无副作用。
        """
        biz = {"trade_no": trade_no, "payment_proof": payment_proof}
        if client_session:
            biz["client_session"] = client_session
        node = self._call("alipay.aipay.agent.payment.verify", biz, timeout=20.0)
        return VerifyResult(
            trade_no=str(node.get("trade_no") or node.get("tradeNo") or trade_no),
            out_trade_no=str(node.get("out_trade_no") or node.get("outTradeNo") or ""),
            resource_id=str(node.get("resource_id") or node.get("resourceId") or ""),
            amount=str(node.get("amount") or ""),
            active=bool(node.get("active")),
        )

    def fulfillment_confirm(self, trade_no: str) -> bool:
        """履约回执（资源交付后异步调用；失败仅记日志，不影响已交付结果）。

        T2 实证：沙箱 fulfillment.confirm 耗时可超 10s（verify 数秒内返回），
        此接口单独放宽到 30s。
        """
        try:
            self._call(
                "alipay.aipay.agent.fulfillment.confirm",
                {"trade_no": trade_no},
                timeout=30.0,
            )
            return True
        except AlipayGatewayError as e:
            # 买家 CLI 可能已代发回执 → TRADE_STATUS_INVALID 视为幂等成功
            if e.sub_code == "TRADE_STATUS_INVALID":
                logger.info("fulfillment already confirmed by buyer side: trade_no=%s", trade_no)
                return True
            logger.warning(
                "fulfillment confirm failed: trade_no=%s code=%s detail=%s",
                trade_no,
                e.sub_code,
                e.internal,
            )
            return False
