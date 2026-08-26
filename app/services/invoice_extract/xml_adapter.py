"""数电票 XML 适配器——结构化 schema 直读，100% 精确。

字段映射移植自一期前端解析核 apps/web/src/lib/xml-invoice.ts（已经 5 张真实票验证）：
- 根节点 <EInvoice>，数据段 <EInvoiceData>，监管信息 <TaxSupervisionInfo>；
- 票种 Header/InherentLabel/GeneralOrSpecialVAT/LabelName；红字标志 InIssuType/LabelCode=="N"；
- 明细 IssuItemInformation 可重复；连字符标签如 TotalTax-includedAmount 原生支持。
"""
import xml.etree.ElementTree as ET

from app.models.invoice_extract import InvoiceRecord
from app.services.invoice_extract.base import (
    InvoiceExtractError,
    clean_amount,
    normalize_cn_date,
    validate_record,
)


def _first_text(root: ET.Element, tag: str) -> str:
    """取首个同名标签的文本（iter 全深度查找，语义等同 DOMParser.getElementsByTagName）。"""
    for el in root.iter(tag):
        if el.text is not None and el.text.strip():
            return el.text.strip()
        # 文本可能分布在子节点/tail（罕见），兜底拼接
        joined = "".join(el.itertext()).strip()
        if joined:
            return joined
    return ""


def parse_xml(xml_bytes: bytes, source_file: str) -> InvoiceRecord:
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        raise InvoiceExtractError(f"文件不是合法的 XML：{e}") from e

    if _first_text(root, "EInvoice") == "" and root.tag != "EInvoice":
        raise InvoiceExtractError("未找到 EInvoice 根节点——这不是数电票 XML 文件")

    invoice_number = _first_text(root, "InvoiceNumber") or _first_text(root, "EIid")
    if not invoice_number:
        # InvoiceNumber 在 TaxSupervisionInfo 内，全树查找等价且更宽容
        raise InvoiceExtractError("未找到发票号码——这不是数电票 XML 文件")

    # 票种在容器节点 GeneralOrSpecialVAT 的子节点 LabelName 里
    vat_kind = None
    for el in root.iter("GeneralOrSpecialVAT"):
        label_name = _first_text(el, "LabelName")
        vat_kind = label_name or None
        break
    if not vat_kind:
        vat_kind = "普通发票"
    invoice_type = f"电子发票（{vat_kind}）"

    is_red = False
    for el in root.iter("InIssuType"):
        is_red = (_first_text(el, "LabelCode") == "N")
        break

    issue_date = normalize_cn_date(_first_text(root, "IssueTime"))

    def party(kind: str) -> "tuple[Optional[str], Optional[str]]":
        name = _first_text(root, f"{kind}Name")
        tax_id = _first_text(root, f"{kind}IdNum")
        return (name or None), (tax_id or None)

    buyer_name, buyer_tax_id = party("Buyer")
    seller_name, seller_tax_id = party("Seller")

    amount_without_tax = clean_amount(_first_text(root, "TotalAmWithoutTax"))
    tax_amount = clean_amount(_first_text(root, "TotalTaxAm"))
    total_with_tax = clean_amount(_first_text(root, "TotalTax-includedAmount"))
    total_in_chinese = _first_text(root, "TotalTax-includedAmountInChinese")
    remark = _first_text(root, "Remark")

    item_count = sum(1 for _ in root.iter("IssuItemInformation")) or None

    warnings: list[str] = []
    if is_red:
        warnings.append("红字发票（冲销/红冲）")
    if not remark and total_in_chinese:
        remark = None  # 大写金额不进备注列（已有价税合计列）

    rec = InvoiceRecord(
        source_file=source_file,
        source_format="xml",
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
        remark=remark or None,
        warnings=warnings,
    )
    validate_record(rec)
    return rec
