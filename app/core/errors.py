"""错误消息净化工具（审计 P3 英文错误族）。

约定：service 层我们自己抛的 ValueError 全部是中文文案；而纯 ASCII 的
ValueError 消息基本来自库层（fitz/pypdf/PyPDF 的 "document closed or
encrypted"、内置 int() 的 "invalid literal for int()" 等），直出会泄露
实现细节且不友好。统一经 friendly_detail() 过滤：含非 ASCII（即含中文）
的原样返回，否则给通用兜底文案。
"""

_DEFAULT_FALLBACK = "处理失败，请检查文件后重试"


def friendly_detail(error: Exception, fallback: str = _DEFAULT_FALLBACK) -> str:
    """返回适合直接展示给用户的错误文案。

    - 消息含中文（非 ASCII）→ 认定是我们精心编写的业务文案，原样返回；
    - 纯 ASCII → 认定是库层英文异常，返回兜底文案（完整信息应已入日志）。
    """
    msg = str(error).strip()
    if msg and not msg.isascii():
        return msg
    return fallback
