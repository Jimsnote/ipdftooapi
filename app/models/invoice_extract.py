"""发票信息批量提取（二期）数据模型。

设计依据：docs/INVOICE_EXTRACT_PHASE2_PLAN.md §3/§4
- 一行一票粒度（明细数单列入列，不展开明细行）；
- 金额全程字符串透传（不浮点化，避免精度问题），仅做 ¥/,/空格 清洗；
- "校验提示"列 = warnings 合并文本，导出默认保留。
"""
from typing import List, Optional

from pydantic import BaseModel, Field


class InvoiceRecord(BaseModel):
    """统一发票记录——三个格式适配器（XML/OFD/PDF）的共同输出。"""

    source_file: str = ""  # 来源文件名（含扩展名）
    source_format: str = ""  # xml | ofd | pdf
    invoice_type: Optional[str] = None  # 电子发票（普通发票）/（增值税专用发票）
    invoice_number: Optional[str] = None  # 发票号码（20 位）
    issue_date: Optional[str] = None  # 开票日期，统一 YYYY-MM-DD
    buyer_name: Optional[str] = None
    buyer_tax_id: Optional[str] = None
    seller_name: Optional[str] = None
    seller_tax_id: Optional[str] = None
    amount_without_tax: Optional[str] = None
    tax_amount: Optional[str] = None
    total_with_tax: Optional[str] = None
    item_count: Optional[int] = None  # 明细行数（取不到为 None，单元格留空）
    remark: Optional[str] = None
    warnings: List[str] = Field(default_factory=list)  # 校验提示（交叉校验/去重提示）


class ExtractFailureItem(BaseModel):
    file: str
    reason: str


class AnalyzeResponse(BaseModel):
    records: List[InvoiceRecord]
    failures: List[ExtractFailureItem]
    success_count: int
    failure_count: int


class ExportRowRequest(BaseModel):
    """前端表格当前态的一行（允许手动补录/修正后的数据）。"""

    source_file: Optional[str] = None
    source_format: Optional[str] = None
    invoice_type: Optional[str] = None
    invoice_number: Optional[str] = None
    issue_date: Optional[str] = None
    buyer_name: Optional[str] = None
    buyer_tax_id: Optional[str] = None
    seller_name: Optional[str] = None
    seller_tax_id: Optional[str] = None
    amount_without_tax: Optional[str] = None
    tax_amount: Optional[str] = None
    total_with_tax: Optional[str] = None
    item_count: Optional[int] = None
    remark: Optional[str] = None
    warnings: Optional[List[str]] = None


class ExportRequest(BaseModel):
    format: str = "xlsx"  # xlsx | csv
    rows: List[ExportRowRequest]


class ExportResponse(BaseModel):
    download_url: str
    filename: str
    count: int
