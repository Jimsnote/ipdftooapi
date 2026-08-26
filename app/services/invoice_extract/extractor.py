"""格式分发入口：按扩展名路由到对应适配器。

analyze 阶段全程内存操作（不落盘）：XML 直接解析，PDF/OFD 走字节流。
"""
from app.models.invoice_extract import InvoiceRecord
from app.services.invoice_extract.base import InvoiceExtractError

SUPPORTED_EXTENSIONS = (".xml", ".ofd", ".pdf")


def extract_bytes(data: bytes, source_file: str, ext: str) -> InvoiceRecord:
    """按扩展名提取单张发票。ext 形如 '.xml'（小写）。"""
    ext = ext.lower()
    if ext == ".xml":
        from app.services.invoice_extract.xml_adapter import parse_xml

        return parse_xml(data, source_file)
    if ext == ".pdf":
        from app.services.invoice_extract.pdf_adapter import extract_pdf_bytes

        return extract_pdf_bytes(data, source_file)
    if ext == ".ofd":
        from app.services.invoice_extract.ofd_adapter import extract_ofd_bytes

        return extract_ofd_bytes(data, source_file)
    raise InvoiceExtractError(f"不支持的文件类型：{ext}")
