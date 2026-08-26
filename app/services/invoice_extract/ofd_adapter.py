"""数电票 OFD 适配器——优先结构化标签，坐标分区兜底。

实测发现（docs/xml-invoice/ofd/ 四张真票）：
- 税局开具的数电票 OFD 在 Doc_0/Tags/CustomTag.xml 内置了**结构化字段索引**：
  InvoiceNo / IssueDate / Buyer(BuyerName,BuyerTaxID) / Seller(...) /
  TaxExclusiveTotalAmount / TaxTotalAmount / TaxInclusiveTotalAmount ...
  每个字段的 <ofd:ObjectRef> 文本即页面 Content.xml 中 TextObject 的 ID，
  因此可"按引用精确取值"（可靠性等同 XML，远好于正则）。
- 注意：金额字段解析出两个引用，第一个是"¥"符号、第二个才是数字。
- 少数变体（如机票电子发票）没有 CustomTag.xml → 回退到与 PDF 适配器同思路的
  内容模式识别 + 官方版式坐标分区（单位 mm，页面 210×140）。

命名空间无关处理：统一按 local-name 匹配，兼容带/不带 ofd: 前缀的变体。
"""
import io
import re
import zipfile
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

from app.models.invoice_extract import InvoiceRecord
from app.services.invoice_extract.base import (
    TAX_ID_RE,
    InvoiceExtractError,
    clean_amount,
    normalize_cn_date,
    validate_record,
)

_NS = "{http://www.ofdspec.org/2016}"


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _find_children(el: ET.Element, name: str) -> List[ET.Element]:
    return [c for c in el.iter() if _local(c.tag) == name]


AmountSpanRe = re.compile(r"^[¥￥]?\s*(\d[\d,]*(?:\.\d{1,2})?)$")
CNDateRe = re.compile(r"^\d{4}年\d{1,2}月\d{1,2}日$")


class _OfdText:
    """TextObject 集合：ID → 文本；以及回退用的坐标信息。"""

    def __init__(self):
        self.id_text: Dict[str, str] = {}
        # (x_center_mm, y_center_mm, text)
        self.placed: List[Tuple[float, float, str]] = []
        self.page_width_mm: float = 210.0


def _load_ofd_stream(data: bytes) -> zipfile.ZipFile:
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001
        raise InvoiceExtractError(f"无法打开 OFD（不是有效的 ZIP 包）：{e}") from e
    names = zf.namelist()
    if not any(n.upper() == "OFD.XML" for n in names):
        raise InvoiceExtractError("包内缺少 OFD.xml——这不是 OFD 版式文件")
    return zf


def extract_ofd_bytes(data: bytes, source_file: str) -> InvoiceRecord:
    """字节流入口（analyze 阶段全程不落盘）。"""
    return parse_ofd_zip(_load_ofd_stream(data), source_file)


def parse_ofd(path: str, source_file: str) -> InvoiceRecord:
    """文件路径入口（测试/脚本便捷封装）。"""
    with open(path, "rb") as f:
        return extract_ofd_bytes(f.read(), source_file)


def parse_ofd_zip(zf: zipfile.ZipFile, source_file: str) -> InvoiceRecord:
    texts = _collect_text_objects(zf)
    custom_tag_name = next(
        (n for n in zf.namelist() if re.match(r"Doc_\d+/Tags/CustomTag\.xml$", n)), None
    )
    rec = None
    if custom_tag_name:
        try:
            tag_root = ET.fromstring(zf.read(custom_tag_name))
        except ET.ParseError as e:
            raise InvoiceExtractError(f"CustomTag.xml 解析失败：{e}") from e
        rec = _extract_via_custom_tag(tag_root, texts, source_file)
    if rec is None:
        rec = _extract_by_zone(texts, source_file)
    # 备注（两条路径通用）：备注区剩余文本（数电票版式备注区约 y 105~128mm）
    if rec.remark is None:
        parts = [
            t
            for x, y, t in sorted(texts.placed, key=lambda s: (s[1], s[0]))
            if 105 <= y < 130 and t not in ("备", "注") and not t.startswith("开票人")
        ]
        rec.remark = " ".join(parts).strip() or None
    return rec


def _collect_text_objects(zf: zipfile.ZipFile) -> _OfdText:
    out = _OfdText()
    for name in zf.namelist():
        # 页面层取值；模板层（Tpls）只并入 id_text——标题等静态文字（发票类型）在模板里
        is_page = re.search(r"Doc_\d+/Pages/Page_\d+/Content\.xml$", name)
        is_tpl = re.search(r"Doc_\d+/Tpls/Tpl_\d+/Content\.xml$", name)
        if not (is_page or is_tpl):
            continue
        try:
            root = ET.fromstring(zf.read(name))
        except ET.ParseError:
            continue
        box = None
        for el in root.iter():
            if _local(el.tag) == "PhysicalBox" and el.text:
                box = el.text.split()
                break
        pw = float(box[2]) if box and len(box) >= 3 else 210.0
        out.page_width_mm = pw
        for t in _find_children(root, "TextObject"):
            tid = t.get("ID")
            codes = [c for c in t.iter() if _local(c.tag) == "TextCode"]
            text = "".join((c.text or "") for c in codes).strip()
            if tid and text:
                out.id_text[tid] = text
            boundary = t.get("Boundary")
            if is_page and boundary and text:
                parts = boundary.replace(",", " ").split()
                if len(parts) >= 4:
                    try:
                        bx, by, bw, bh = (float(p) for p in parts[:4])
                        out.placed.append((bx + bw / 2, by + bh / 2, text))
                    except ValueError:
                        pass
    return out


def _resolve_refs(field_el: ET.Element, idmap: Dict[str, str]) -> List[str]:
    vals: List[str] = []
    for ref in _find_children(field_el, "ObjectRef"):
        rid = (ref.text or "").strip()
        if rid and rid in idmap:
            vals.append(idmap[rid])
    return vals


def _amount_from_refs(vals: List[str]) -> Optional[str]:
    """金额字段引用序列形如 ['¥', '695086.79']——取其中数字样貌的那个。"""
    for v in reversed(vals):
        cleaned = clean_amount(v)
        if cleaned is not None:
            return cleaned
    return None


# ---------------------------------------------------------------------------
# 路径 A：CustomTag 结构化引用（精确）
# ---------------------------------------------------------------------------

_FIELD_MAP = {
    "InvoiceNo": "number",
    "IssueDate": "date",
    "BuyerName": "buyer_name",
    "BuyerTaxID": "buyer_tax_id",
    "SellerName": "seller_name",
    "SellerTaxID": "seller_tax_id",
}


def _extract_via_custom_tag(
    tag_root: ET.Element, texts: _OfdText, source_file: str
) -> Optional[InvoiceRecord]:
    got: Dict[str, str] = {}
    amounts: Dict[str, Optional[str]] = {}
    has_ref = False
    for el in tag_root.iter():
        name = _local(el.tag)
        if name in ("root", "ObjectRef", "Buyer", "Seller"):
            continue
        refs = _resolve_refs(el, texts.id_text)
        if not refs:
            continue
        has_ref = True
        joined = "".join(refs)
        if name in _FIELD_MAP:
            got[_FIELD_MAP[name]] = joined.strip()
        elif name == "IssueDate":
            got["date"] = joined.strip()
        elif name == "TaxExclusiveTotalAmount":
            amounts["without"] = _amount_from_refs(refs)
        elif name == "TaxTotalAmount":
            amounts["tax"] = _amount_from_refs(refs)
        elif name == "TaxInclusiveTotalAmount":
            amounts["total"] = _amount_from_refs(refs)

    if not has_ref:
        return None

    warnings: List[str] = []
    if any("红字" in v for v in texts.id_text.values()):
        warnings.append("疑似红字发票（冲销/红冲），请核对")
    rec = _detect_type_and_build(texts, got, amounts, warnings, source_file)
    validate_record(rec)
    return rec


def _detect_invoice_type(texts: "_OfdText") -> Optional[str]:
    """标题在模板层且可能被拆成多个 TextObject：全池拼接后正则 + 子串兜底。"""
    pool = "".join(texts.id_text.values())
    m = re.search(r"电子发票（([^（）]{2,15})）", pool)
    if m:
        return f"电子发票（{m.group(1)}）"
    if "增值税专用发票" in pool:
        return "电子发票（增值税专用发票）"
    if "普通发票" in pool:
        return "电子发票（普通发票）"
    return None


def _detect_type_and_build(
    texts: "_OfdText",
    got: Dict[str, str],
    amounts: Dict[str, Optional[str]],
    warnings: List[str],
    source_file: str,
) -> InvoiceRecord:
    invoice_type = _detect_invoice_type(texts)
    return InvoiceRecord(
        source_file=source_file,
        source_format="ofd",
        invoice_type=invoice_type,
        invoice_number=got.get("number") or None,
        issue_date=normalize_cn_date(got.get("date")),
        buyer_name=got.get("buyer_name") or None,
        buyer_tax_id=got.get("buyer_tax_id") or None,
        seller_name=got.get("seller_name") or None,
        seller_tax_id=got.get("seller_tax_id") or None,
        amount_without_tax=amounts.get("without"),
        tax_amount=amounts.get("tax"),
        total_with_tax=amounts.get("total"),
        item_count=None,  # CustomTag 的 Item 引用是拆分的文本片段，非行数
        remark=None,  # 备注未被标签索引，v1 留空
        warnings=list(warnings),
    )


# ---------------------------------------------------------------------------
# 路径 B：无 CustomTag 的回退——内容模式 + 版式坐标分区（单位 mm）
# ---------------------------------------------------------------------------

# 注意：不能用"信息"等公司名词做排除（"XX信息技术有限公司"会被误杀）；
# 水印单字与标签由长度/剥离规则过滤。
_NAME_EXCLUDE = ("名称", "代码", "识别号", "发票", "合计", "备注", "开票人", "价税")


def _extract_by_zone(texts: _OfdText, source_file: str) -> InvoiceRecord:
    warnings: List[str] = []
    placed = texts.placed
    mid_x = texts.page_width_mm / 2

    def zone(y_lo: float, y_hi: float, x_lo: float = -1.0, x_hi: float = 1e9):
        hits = [(x, y, t) for (x, y, t) in placed if y_lo <= y < y_hi and x_lo <= x <= x_hi]
        return sorted(hits, key=lambda s: s[0])

    number = next((t for _, _, t in placed if re.fullmatch(r"\d{20}", t)), None)
    date_candidates = [t for _, y, t in placed if CNDateRe.match(t)]
    issue_date = normalize_cn_date(min(date_candidates, key=len)) if date_candidates else None

    buyer_tax_id = seller_tax_id = None
    for x, _, t in zone(38, 60):
        if t != number and TAX_ID_RE.match(t):
            if x < mid_x and buyer_tax_id is None:
                buyer_tax_id = t
            elif x >= mid_x and seller_tax_id is None:
                seller_tax_id = t

    def _strip_label(t: str) -> str:
        """有的变体把「名称：」标签和值粘在同一文本对象里，先剥前缀再判。"""
        return re.sub(r"^名称[：:]\s*", "", t)

    def looks_like_name(t: str) -> bool:
        t = _strip_label(t)
        return (
            len(t) >= 4
            and re.search(r"[\u4e00-\u9fff]", t)
            and not any(k in t for k in _NAME_EXCLUDE)
            and not TAX_ID_RE.match(t)
            and not AmountSpanRe.match(t)
        )

    name_hits = [s for s in zone(26, 46) if looks_like_name(s[2])]
    buyer_name = seller_name = None
    left = [s for s in name_hits if s[0] < mid_x]
    right = [s for s in name_hits if s[0] >= mid_x]
    if left:
        buyer_name = _strip_label(max(left, key=lambda s: len(s[2]))[2])
    if right:
        seller_name = _strip_label(max(right, key=lambda s: len(s[2]))[2])

    # 合计行（约 y 90~97）：两个金额按 x 排序
    amount_without_tax = tax_amount = total_with_tax = None
    totals = [s for s in zone(88, 98) if clean_amount(s[2]) is not None]
    if len(totals) >= 2:
        amount_without_tax = clean_amount(totals[0][2])
        tax_amount = clean_amount(totals[-1][2])
    # 价税合计行（约 y 96~112，右侧）：取最靠右的金额
    vat = [s for s in zone(96, 113, x_lo=texts.page_width_mm * 0.55) if clean_amount(s[2]) is not None]
    if vat:
        total_with_tax = clean_amount(max(vat, key=lambda s: s[0])[2])

    item_count = sum(1 for _, y, t in placed if 55 <= y < 92 and t.startswith("*")) or None

    invoice_type = _detect_invoice_type(texts)
    if any("红字" in t for _, _, t in placed):
        warnings.append("疑似红字发票（冲销/红冲），请核对")

    rec = InvoiceRecord(
        source_file=source_file,
        source_format="ofd",
        invoice_type=invoice_type,
        invoice_number=number,
        issue_date=issue_date,
        buyer_name=buyer_name,
        buyer_tax_id=buyer_tax_id,
        seller_name=seller_name,
        seller_tax_id=seller_tax_id,
        amount_without_tax=amount_without_tax,
        tax_amount=tax_amount,
        total_with_tax=total_with_tax,
        item_count=item_count,
        remark=None,
        warnings=warnings,
    )
    core_missing = [f for f, v in (("发票号码", number), ("价税合计", total_with_tax)) if not v]
    if len(core_missing) >= 2:
        raise InvoiceExtractError(
            "未能从该 OFD 提取关键字段（缺少结构化标签且版式不匹配），暂不支持"
        )
    validate_record(rec)
    return rec
