"""支付宝 AI 按量付费（A402）skillpay 服务包。

- payment.py      402 账单构造 + 商家签名（8 字段 RSA2）+ Payment-Proof 解析
- orders.py       SQLite 订单存取（原子抢占 + 幂等缓存 + 生命周期清理）
- alipay_client.py 开放平台网关客户端（verify / fulfillment.confirm）
"""
from app.services.skillpay.alipay_client import (  # noqa: F401
    AlipayClient,
    AlipayGatewayError,
    VerifyResult,
    map_error_message,
)
from app.services.skillpay.orders import (  # noqa: F401
    Order,
    OrderStore,
)
from app.services.skillpay.payment import (  # noqa: F401
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
    rsa2_sign,
    rsa2_verify,
    skillpay_config_from_settings,
)
