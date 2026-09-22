"""发票提取公共工具：清洗、校验、统一异常。"""
import re
from typing import List, Optional

from app.models.invoice_extract import InvoiceRecord

# 统一社会信用代码/纳税人识别号字符集（18 位标准；兼容 15 位老税号纯数字）
TAX_ID_RE = re.compile(r"^[0-9A-HJ-NPQRTUWXY]{15,20}$")
AMOUNT_RE = re.compile(r"^\d{1,12}(\.\d{1,2})?$")
CN_DATE_RE = re.compile(r"^(\d{4})年(\d{1,2})月(\d{1,2})日$")
ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")


class InvoiceExtractError(Exception):
    """单文件提取失败（路由层捕获后转为 failures 条目，不阻断整批）。"""


def reject_dtd(data: bytes) -> None:
    """拒绝含 DTD/实体声明的 XML（防 billion laughs 实体展开 DoS）。

    xml.etree.ElementTree 对内部实体展开无防护；数电票 XML 与 OFD 内的
    发票 XML 均不含 DTD，此处前置拒绝零误杀。
    """
    head = data[:4096].lstrip(b"\xef\xbb\xbf \t\r\n")
    if b"<!DOCTYPE" in head or b"<!ENTITY" in head or b"<!ENTITY" in data[-4096:]:
        raise InvoiceExtractError("XML 含不支持的 DTD/实体声明")


def clean_amount(raw: Optional[str]) -> Optional[str]:
    """清洗金额：去掉 ¥/￥/,/空格；非数字样貌返回 None。

    "¥ 695,086.79" → "695086.79"；"695086.79" 原样；"abc"/None → None。
    """
    if raw is None:
        return None
    s = str(raw).replace("¥", "").replace("￥", "").replace(",", "").replace(" ", "").strip()
    if not s:
        return None
    if not AMOUNT_RE.match(s):
        return None
    # 规范化小数位（保留原精度，仅去多余前导零）
    try:
        value = float(s)
    except ValueError:
        return None
    return ("%.2f" % value) if "." in s else s


def is_amount(raw: Optional[str]) -> bool:
    return clean_amount(raw) is not None


def normalize_cn_date(raw: Optional[str]) -> Optional[str]:
    """2026年08月25日 / 2026-8-25 → 2026-08-25；解析失败返回 None。"""
    if not raw:
        return None
    s = str(raw).strip()
    m = CN_DATE_RE.match(s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = ISO_DATE_RE.match(s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def _to_float(s: Optional[str]) -> Optional[float]:
    s = clean_amount(s)
    if s is None:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def validate_record(rec: InvoiceRecord) -> None:
    """票内交叉校验，问题写入 warnings（不阻断、不判失败）。"""
    warnings: List[str] = []
    if rec.invoice_number and not re.match(r"^\d{20}$", rec.invoice_number):
        warnings.append("发票号码不是 20 位数字，请核对")
    if rec.issue_date is None and rec.invoice_number:
        warnings.append("未能识别开票日期")
    amount = _to_float(rec.amount_without_tax)
    tax = _to_float(rec.tax_amount)
    total = _to_float(rec.total_with_tax)
    if amount is not None and tax is not None and total is not None:
        if abs(amount + tax - total) > 0.02:
            warnings.append("不含税金额+税额与价税合计不一致，请核对")
    elif total is None and rec.invoice_number:
        warnings.append("未能识别价税合计金额")
    rec.warnings = list(dict.fromkeys((rec.warnings or []) + warnings))
