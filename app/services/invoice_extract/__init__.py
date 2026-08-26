"""发票信息批量提取服务包。

三个格式适配器输出统一的 InvoiceRecord（见 app/models/invoice_extract.py）：
- xml_adapter.py  数电票 XML（结构化，100% 精确；映射移植自一期前端解析核）
- ofd_adapter.py  数电票 OFD（优先 Doc_0/Tags/CustomTag.xml 结构化引用精确取值，
                  无标签时按内容模式 + 版式坐标分区回退）
- pdf_adapter.py  文字层 PDF（PyMuPDF 提取，标签锚定正则；无文字层判扫描件排除）

导出 exporter.py：CSV（UTF-8 BOM）/ xlsx（openpyxl）双格式。
"""
from app.services.invoice_extract.base import (  # noqa: F401
    InvoiceExtractError,
    clean_amount,
    is_amount,
    normalize_cn_date,
    validate_record,
)
from app.services.invoice_extract.exporter import (
    build_filename,  # noqa: F401
    export_csv,  # noqa: F401
    export_xlsx,  # noqa: F401
)
from app.services.invoice_extract.extractor import extract_bytes  # noqa: F401
