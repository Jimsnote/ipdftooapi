"""查重键归一化与号码语义校验（纯函数，可单测）。

apps/web/src/lib/invoiceDedup/keys.ts 的逐函数移植（golden tests 见
tests/test_invoice_dedup.py）。

注意：本模块的 clean_amount 与 app/services/invoice_extract/base.py 的
clean_amount **语义刻意不同，不合并**——
- extract 管线：保留原精度（"270" → "270"），供台账展示；
- dedup 管线：强制两位小数规范化（"270" → "270.00"），查重/台账比对要求
  金额有唯一规范形，否则 "270" vs "270.00" 会误判金额不一致（可疑）。
TS 原版即后者语义，golden tests 以 TS 行为为准。
"""
import re
from datetime import date
from typing import Optional

# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------

# 全角 ！(U+FF01)~～(U+FF5E) → 半角（减 0xFEE0）；随后去除全部空白
def normalize_text(s: Optional[str]) -> str:
    if not s:
        return ""
    out = []
    for ch in s:
        code = ord(ch)
        if 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return re.sub(r"\s+", "", "".join(out))


# ---------------------------------------------------------------------------
# 金额
# ---------------------------------------------------------------------------

_AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")


def clean_amount(raw: Optional[str]) -> Optional[str]:
    """金额清洗：去 ¥￥/空格/千分位逗号（含全角）；非法返回 None。

    "¥736,792.00" → "736792.00"；"￥270" → "270.00"；"1,234.5" → "1234.50"。
    """
    if not raw:
        return None
    s = re.sub(r"[¥￥,，]", "", normalize_text(raw))
    if not _AMOUNT_RE.match(s):
        return None
    if "." in s:
        integer, frac = s.split(".")
        return f"{integer}.{frac.ljust(2, '0')}"
    return f"{s}.00"


def add_amounts(a: Optional[str], b: Optional[str]) -> Optional[str]:
    """分单位金额加法（避免浮点误差），任一为空返回另一个。"""
    if not a and not b:
        return None

    def to_cents(v: str) -> int:
        return round(float(v) * 100)

    cents = (to_cents(a) if a else 0) + (to_cents(b) if b else 0)
    return f"{cents // 100}.{cents % 100:02d}"


# ---------------------------------------------------------------------------
# 号码语义校验
# ---------------------------------------------------------------------------

_YEAR_RE = re.compile(r"^(2[3-8])\d{18}$")  # 年度 23-28（留一年余量）


def is_valid_e_invoice_no(no: Optional[str]) -> bool:
    """20 位数电票号码位段语义：1-2 位年度（23-28）+ 3-4 位省级税局代码（11-65）。"""
    if not no or not _YEAR_RE.match(no):
        return False
    return 11 <= int(no[2:4]) <= 65


def is_classic_no(no: Optional[str]) -> bool:
    """8 位传统票号码。"""
    return bool(no) and re.match(r"^\d{8}$", no) is not None


def is_classic_code(code: Optional[str]) -> bool:
    """10-12 位传统发票代码。"""
    return bool(code) and re.match(r"^\d{10,12}$", code) is not None


def make_key(number: Optional[str], code: Optional[str] = None) -> str:
    """查重键：传统票「代码-号码」，数电票仅号码；号码缺失返回 ""。"""
    no = normalize_text(number)
    if not no:
        return ""
    code_n = normalize_text(code)
    if code_n and is_classic_code(code_n):
        return f"{code_n}-{no}"
    return no


# ---------------------------------------------------------------------------
# 日期
# ---------------------------------------------------------------------------

_FILE_TS_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})")


def date_from_file_name(file_name: Optional[str]) -> Optional[str]:
    """文件名日期兜底：取最后一个下划线分段的前 8 位时间戳，真实日期校验。

    dzfp_号码_公司_20260902095513.pdf → "2026-09-02"；非法返回 None。
    """
    if not file_name:
        return None
    stem = re.sub(r"\.[^.]+$", "", file_name)
    segs = stem.split("_")
    last = segs[-1] if segs else ""
    m = _FILE_TS_RE.match(last)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        date(y, mo, d)
    except ValueError:
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


_CN_DATE_RE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def cn_date_from_text(text: Optional[str]) -> Optional[str]:
    """从任意票面文本中扫开票日期（「2026年08月25日」整体出现，取第一个合法值）。"""
    if not text:
        return None
    for m in _CN_DATE_RE.finditer(text):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            date(y, mo, d)
        except ValueError:
            continue
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


# ---------------------------------------------------------------------------
# Excel 文本化
# ---------------------------------------------------------------------------


def excel_text(s: Optional[str]) -> str:
    """Excel/WPS 文本化导出：`="值"` 公式式文本防 20 位号码科学计数法/精度丢失。

    空值返回空串；值内引号按 CSV 规范双写。
    """
    if not s:
        return ""
    return f'="{s.replace(chr(34), chr(34) * 2)}"'
