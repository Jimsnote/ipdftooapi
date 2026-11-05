from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SurveySubmission(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    frequency: Literal[
        "这是第一次使用",
        "偶尔使用，几个月一次",
        "每月几次",
        "每周几次",
        "几乎每天使用",
    ]
    features: list[str]
    source: Literal[
        "百度搜索",
        "Bing 或其他搜索引擎",
        "小红书",
        "朋友或同事推荐",
        "AI 工具推荐",
        "微信群 / 社群",
        "其他",
    ]
    pricing_model: Literal[
        "希望一直完全免费",
        "按次购买大额任务包",
        "19.9 元终身买断基础 Pro",
        "按月或按年订阅",
        "要看具体功能再决定",
    ]
    price_range: Literal[
        "不考虑付费",
        "9.9 元以内",
        "19.9 元",
        "29.9～39.9 元",
        "50 元及以上",
        "不确定",
    ]
    feature_request: str = Field(default="", max_length=500)
    task_outcome: Literal[
        "一次就成功，结果符合预期",
        "成功了，但操作过程有些复杂",
        "部分成功，结果有需要改进的地方",
        "没有成功",
    ]
    issue_description: str = Field(default="", max_length=500)
    usage_scenario: Literal[
        "个人报销或日常办公",
        "企业财务 / 会计人员",
        "行政、人事或综合管理",
        "开发者 / AI 文档处理",
        "学生或科研学习",
        "自由职业 / 个体经营",
        "其他",
    ]
    contact: str = Field(default="", max_length=100)
    feature_other: str = Field(default="", max_length=100)
    source_other: str = Field(default="", max_length=100)
    scenario_other: str = Field(default="", max_length=100)
    page_path: Literal[
        "/invoice-merge/result",
        "/ofd-to-pdf/result",
        "/merge-pdf/result",
    ]
    client_id: str = Field(pattern=r"^[A-Za-z0-9-]{32,64}$")
    schema_version: Literal["v1"] = "v1"
    elapsed_ms: int = Field(ge=0, le=86_400_000)
    website: str = Field(default="", max_length=200)

    @field_validator("features")
    @classmethod
    def validate_features(cls, value: list[str]) -> list[str]:
        allowed = {
            "发票合并打印",
            "OFD 转 PDF / OFD 在线查看",
            "发票提取 Excel",
            "发票重命名 / 查重",
            "PDF 合并 / 分割 / 压缩",
            "PDF 转 Word",
            "文档转 Markdown",
            "其他",
        }
        if not value or len(value) > len(allowed) or len(set(value)) != len(value):
            raise ValueError("功能选择不合法")
        if any(item not in allowed for item in value):
            raise ValueError("功能选择不合法")
        return value
