"""文字层 PDF 适配器——按数电票官方横版版式做坐标分区提取（PyMuPDF）。

为什么不用"标签锚定正则"：实测官方版式 PDF 的纯文本流是
「标签堆在前、数值堆在后」（阅读序≠视觉序），正则会张冠李戴。
因此用 get_text("dict") 拿每个 span 的精确坐标，按一期逆向测量得到的
版式几何分区取值（页面 210×140mm ≈ 595.3×396.9pt）：

- 标题区   y<48            → 发票类型（电子发票（xx））
- 号码日期 y∈[24,64) x>400 → 发票号码(20位)/开票日期(年月日)
- 名称行   y∈[86,114)      → 购方名称(x中点<298)/销方名称(≥298)
- 税号行   y∈[114,150)     → 匹配信用代码字符集，同上分侧
- 明细区   y∈[150,254)     → 以'*'开头的 span 计明细行数；'*'开头即项目名
- 合计行   y∈[248,272)     → ¥金额两个，按 x 排序 = 不含税金额、税额
- 价税合计 y∈[272,296)     → ¥金额一个 = 价税合计（小写）
- 备注区   y∈[296,354)     → 剩余文本拼备注（排除框外开票人/下载次数）

范围外版式（旧版增值税票等）：核心字段取不到 → 失败项并提示，宁失败不错账。
"""
import re
from typing import List, Optional, Tuple

import fitz

from app.models.invoice_extract import InvoiceRecord
from app.services.invoice_extract.base import (
    TAX_ID_RE,
    InvoiceExtractError,
    clean_amount,
    normalize_cn_date,
    validate_record,
)

# 分区边界（pt），源自官方版式逆向测量
Y_TITLE_MAX = 48.0
Y_NUMDATE = (24.0, 64.0)
X_NUMDATE_MIN = 400.0
Y_NAME = (86.0, 114.0)
Y_TAXID = (114.0, 150.0)
PARTY_MID_X = 298.0
Y_ITEMS = (150.0, 254.0)
Y_TOTALS = (248.0, 272.0)
Y_VAT = (272.0, 296.0)
Y_REMARK = (296.0, 354.0)

# ¥ 与数字是不同字体，PyMuPDF 会拆成两个 span → 货币符号可选
AMOUNT_SPAN_RE = re.compile(r"^[¥￥]?\s*(\d[\d,]*(?:\.\d{1,2})?)$")
CN_DATE_SPAN_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")

# 某些 PDF 会把「标签：值」排进同一 span，取值前先剥已知前缀
_LABEL_PREFIX_RE = re.compile(
    r"^(?:发票号码|开票日期|名称|统一社会信用代码/?纳税人识别号)[：:]\s*"
)


def _strip_label(text: str) -> str:
    return _LABEL_PREFIX_RE.sub("", text).strip()


def _collect_spans(doc: "fitz.Document") -> List[Tuple[float, float, float, float, str]]:
    """收集全部页面的文本 span：返回 [(x0, y0, x1, y1, text)]（绝对 pt 坐标）。"""
    spans: List[Tuple[float, float, float, float, str]] = []
    for page in doc:
        pw = page.rect.width
        scale = 595.3 / pw if pw > 0 else 1.0  # 容错非标准宽度页面
        raw = page.get_text("dict")
        for block in raw.get("blocks", []):
            for line in block.get("lines", []):
                for sp in line.get("spans", []):
                    text = (sp.get("text") or "").strip()
                    if not text:
                        continue
                    x0, y0, x1, y1 = sp["bbox"]
                    spans.append((x0 * scale, y0 * scale, x1 * scale, y1 * scale, text))
    return spans


def _in_band(y0: float, y1: float, band: Tuple[float, float]) -> bool:
    """span 与纵向区间的重叠超过其自身高度一半才视为落在该区。"""
    mid = (y0 + y1) / 2
    return band[0] <= mid < band[1]


def _pick(spans, band, pred=None):
    hits = [s for s in spans if _in_band(s[1], s[3], band) and (pred is None or pred(s))]
    return hits


def extract_pdf_bytes(data: bytes, source_file: str) -> InvoiceRecord:
    """字节流入口（analyze 阶段全程不落盘）。"""
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:  # noqa: BLE001
        raise InvoiceExtractError(f"无法打开 PDF：{e}") from e
    try:
        full_text = "".join(page.get_text("text") for page in doc)
        if len(full_text.strip()) < 50:
            raise InvoiceExtractError(
                "未检测到文字层——这是扫描件/图片型 PDF。请先通过「OCR 工具」转为可搜索 PDF 后重试"
            )
        spans = _collect_spans(doc)
        return _extract_from_spans(spans, full_text, source_file)
    finally:
        doc.close()


def parse_pdf(path: str, source_file: str) -> InvoiceRecord:
    """文件路径入口（测试/脚本便捷封装）。"""
    with open(path, "rb") as f:
        return extract_pdf_bytes(f.read(), source_file)


def _extract_from_spans(
    spans: List[Tuple[float, float, float, float, str]],
    full_text: str,
    source_file: str,
) -> InvoiceRecord:
    warnings: List[str] = []

    # —— 发票类型（标题区）
    invoice_type: Optional[str] = None
    m = re.search(r"电子发票（([^（）]{2,15})）", full_text)
    if m:
        invoice_type = f"电子发票（{m.group(1)}）"

    # —— 发票号码 / 开票日期
    invoice_number: Optional[str] = None
    issue_date: Optional[str] = None
    for x0, y0, x1, y1, text in _pick(spans, Y_NUMDATE, lambda s: s[0] >= X_NUMDATE_MIN):
        clean = _strip_label(text)
        if invoice_number is None:
            m = re.search(r"\d{20}", clean)
            if m:
                invoice_number = m.group(0)
                continue
        if issue_date is None:
            m = CN_DATE_SPAN_RE.search(clean)
            if m:
                issue_date = normalize_cn_date(f"{m.group(1)}年{m.group(2)}月{m.group(3)}日")

    # —— 购销方名称 / 税号
    def side_of(span: Tuple[float, float, float, float, str]) -> str:
        cx = (span[0] + span[2]) / 2
        return "buyer" if cx < PARTY_MID_X else "seller"

    buyer_name = seller_name = None
    # 注意不能用关键词排除公司名（如"XX信息技术有限公司"含"信息"）；
    # 水印单字与"名称："标签分别被长度条件与前缀剥离过滤。
    name_hits = _pick(spans, Y_NAME, lambda s: len(_strip_label(s[4])) >= 4)
    # 同侧多个 span（长名称被拆行）时合并：按 y 再分组太复杂，v1 取每侧最宽的一个
    for side_key in ("buyer", "seller"):
        cand = [s for s in name_hits if side_of(s) == side_key]
        if cand:
            best = max(cand, key=lambda s: s[2] - s[0])
            val = _strip_label(best[4])
            if side_key == "buyer":
                buyer_name = val
            else:
                seller_name = val

    buyer_tax_id = seller_tax_id = None
    for x0, y0, x1, y1, text in _pick(spans, Y_TAXID):
        clean = _strip_label(text)
        if not TAX_ID_RE.match(clean):
            continue
        if side_of((x0, y0, x1, y1, text)) == "buyer":
            buyer_tax_id = buyer_tax_id or clean
        else:
            seller_tax_id = seller_tax_id or clean

    # —— 金额三兄弟（分区优先，全文本正则兜底：某些 PDF 会把标签与金额并进同一 span）
    amount_without_tax = tax_amount = total_with_tax = None
    totals = _pick(spans, Y_TOTALS, lambda s: AMOUNT_SPAN_RE.match(s[4]))
    if len(totals) >= 2:
        ordered = sorted(totals, key=lambda s: s[0])
        amount_without_tax = clean_amount(AMOUNT_SPAN_RE.match(ordered[0][4]).group(1))
        tax_amount = clean_amount(AMOUNT_SPAN_RE.match(ordered[-1][4]).group(1))
    else:
        m = re.search(
            r"合\s*计\s*[¥￥]?\s*(\d[\d,]*\.\d{2})\s*[¥￥]?\s*(\d[\d,]*\.\d{2})", full_text
        )
        if m:
            amount_without_tax = clean_amount(m.group(1))
            tax_amount = clean_amount(m.group(2))
    vat = _pick(spans, Y_VAT, lambda s: AMOUNT_SPAN_RE.match(s[4]))
    if vat:
        best = max(vat, key=lambda s: s[0])  # （小写）¥ 在该行右侧
        total_with_tax = clean_amount(AMOUNT_SPAN_RE.match(best[4]).group(1))
    else:
        m = re.search(r"[（(]\s*小写\s*[)）]\s*[¥￥]?\s*(\d[\d,]*\.\d{2})", full_text)
        if m:
            total_with_tax = clean_amount(m.group(1))

    # —— 明细行数（以 *税收分类* 开头的项目名）
    item_count = sum(1 for s in _pick(spans, Y_ITEMS) if s[4].startswith("*")) or None

    # —— 备注（备注区内剩余文本）
    remark_parts = [
        s[4]
        for s in _pick(spans, Y_REMARK)
        if not re.fullmatch(r"[备注]\s*", s[4]) and "下载次数" not in s[4] and not AMOUNT_SPAN_RE.match(s[4])
    ]
    remark = " ".join(remark_parts).strip() or None

    # —— 红字提示
    if "红字" in full_text[:200] or "红字" in (invoice_type or ""):
        warnings.append("疑似红字发票（冲销/红冲），请核对")

    rec = InvoiceRecord(
        source_file=source_file,
        source_format="pdf",
        invoice_type=invoice_type,
        invoice_number=invoice_number,
        issue_date=issue_date,
        buyer_name=buyer_name,
        buyer_tax_id=buyer_tax_id,
        seller_name=seller_name,
        seller_tax_id=seller_tax_id,
        amount_without_tax=amount_without_tax,
        tax_amount=tax_amount,
        total_with_tax=total_with_tax,
        item_count=item_count,
        remark=remark,
        warnings=warnings,
    )

    # 宁失败不错账：核心三要素全缺视为不支持的版式
    core_missing = [f for f, v in (
        ("发票号码", rec.invoice_number), ("价税合计", rec.total_with_tax)
    ) if not v]
    if len(core_missing) >= 2:
        raise InvoiceExtractError(
            "未能从该 PDF 定位数电票关键字段（" + "、".join(core_missing) +
            "）——可能不是数电票标准横版版式，暂不支持"
        )
    validate_record(rec)
    return rec
