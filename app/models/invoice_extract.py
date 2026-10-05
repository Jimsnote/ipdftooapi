"""发票信息批量提取（二期）数据模型。

设计依据：docs/INVOICE_EXTRACT_PHASE2_PLAN.md §3/§4
- 一行一票粒度（明细数单列入列，不展开明细行）；
- 金额全程字符串透传（不浮点化，避免精度问题），仅做 ¥/,/空格 清洗；
- "校验提示"列 = warnings 合并文本，导出默认保留。
"""
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

# 审计 #21：单元格长度上限（此前无限制，单行字段可塞 MB 级字符串，
# 10000 行上限 × 无行长限制 → xlsx 全内存构建 OOM 压力）
CELL_MAX_LENGTH = 500
MAX_WARNINGS_PER_ROW = 20


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
    """前端表格当前态的一行（允许手动补录/修正后的数据）。

    审计 #21：字符串字段统一 max_length=500、warnings 条数与单条长度受限，
    防止单行 MB 级字段把 xlsx 全内存构建撑爆。
    """

    source_file: Optional[str] = Field(None, max_length=CELL_MAX_LENGTH)
    source_format: Optional[str] = Field(None, max_length=CELL_MAX_LENGTH)
    invoice_type: Optional[str] = Field(None, max_length=CELL_MAX_LENGTH)
    invoice_number: Optional[str] = Field(None, max_length=CELL_MAX_LENGTH)
    issue_date: Optional[str] = Field(None, max_length=CELL_MAX_LENGTH)
    buyer_name: Optional[str] = Field(None, max_length=CELL_MAX_LENGTH)
    buyer_tax_id: Optional[str] = Field(None, max_length=CELL_MAX_LENGTH)
    seller_name: Optional[str] = Field(None, max_length=CELL_MAX_LENGTH)
    seller_tax_id: Optional[str] = Field(None, max_length=CELL_MAX_LENGTH)
    amount_without_tax: Optional[str] = Field(None, max_length=CELL_MAX_LENGTH)
    tax_amount: Optional[str] = Field(None, max_length=CELL_MAX_LENGTH)
    total_with_tax: Optional[str] = Field(None, max_length=CELL_MAX_LENGTH)
    item_count: Optional[int] = None
    remark: Optional[str] = Field(None, max_length=CELL_MAX_LENGTH)
    warnings: Optional[List[str]] = Field(
        None,
        max_length=MAX_WARNINGS_PER_ROW,
    )

    @field_validator("warnings")
    @classmethod
    def _limit_warning_length(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v and any(len(w) > CELL_MAX_LENGTH for w in v):
            raise ValueError("单条校验提示过长")
        return v


class ExportRequest(BaseModel):
    format: str = "xlsx"  # xlsx | csv
    rows: List[ExportRowRequest]


class ExportResponse(BaseModel):
    download_url: str
    filename: str
    count: int


class RenameSkippedItem(BaseModel):
    """批量重命名中未识别（保留原名进 ZIP）的文件。"""

    file: str
    reason: str


class RenameBatchResponse(BaseModel):
    """POST /invoice/rename-batch 响应。"""

    task_id: str
    download_url: str
    total: int
    renamed: int
    duplicates_resolved: int
    skipped: List[RenameSkippedItem]
