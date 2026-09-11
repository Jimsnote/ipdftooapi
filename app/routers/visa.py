# -*- coding: utf-8 -*-
"""XFA 签证表回填路由：POST /api/v1/visa/fill。

设计要点（docs/XFA_PHASE_1_HANDOFF.md §三）：
- 请求体含护照号等敏感个人信息：不写日志（不 log 请求体/字段值）、
  服务端纯内存处理即返回，不落盘；
- 并发管控：asyncio.Semaphore(2)，与 OFD 转换同规格（2C2G 内存预算）；
  CPU 密集的回填放线程池，避免阻塞事件循环；
- 校验类错误（未知模板/非法 SOM/超长值）→ 422；
  完整性自检失败（服务端内部问题）→ 500，绝不发出坏文件；
- 不加 CORS "*"：生产同源部署（apps/web 经 Nginx 反代）。
"""

import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from app.core.logger import get_logger
from app.services import visa_fill
from app.services.visa_fill import (
    InvalidFieldValueError,
    InvalidSOMPathError,
    UnknownTemplateError,
    ValueTooLongError,
)

logger = get_logger(__name__)

router = APIRouter()

# 单 worker 前提下的进程级并发上限（与 ofd_validator 同规格，扩容前必须重估）
_CONCURRENCY = 2
_semaphore = asyncio.Semaphore(_CONCURRENCY)

MSG_UNKNOWN_TEMPLATE = "暂不支持该表格模板"
MSG_INVALID_SOM = "包含无效的表单字段路径，请刷新页面后重试"
MSG_VALUE_TOO_LONG = "部分字段内容过长，请缩短后重试"
MSG_INVALID_CHARS = "部分字段内容含不支持的字符（控制/二进制字符），请重新输入"
MSG_INTERNAL = "服务器处理失败，请稍后重试"


class VisaFillRequest(BaseModel):
    template: str
    values: dict[str, str]


@router.post("/fill")
async def fill_visa(body: VisaFillRequest) -> Response:
    try:
        async with _semaphore:
            # CPU 密集（pikepdf 解析 + AES 加密）放线程池；字段值只在
            # 内存中流转，不落盘不写日志
            out = await asyncio.to_thread(
                visa_fill.fill_visa_form, body.template, dict(body.values)
            )
    except UnknownTemplateError:
        raise HTTPException(status_code=422, detail=MSG_UNKNOWN_TEMPLATE)
    except InvalidSOMPathError:
        raise HTTPException(status_code=422, detail=MSG_INVALID_SOM)
    except ValueTooLongError:
        raise HTTPException(status_code=422, detail=MSG_VALUE_TOO_LONG)
    except InvalidFieldValueError:
        raise HTTPException(status_code=422, detail=MSG_INVALID_CHARS)
    except RuntimeError as e:
        # 完整性自检失败等内部错误：不向客户端泄露细节
        logger.error(f"visa fill internal error: template={body.template}: {e}")
        raise HTTPException(status_code=500, detail=MSG_INTERNAL)

    # 仅记录非敏感元信息（模板 ID 与字节数），不记录字段值
    logger.info(f"visa fill completed: template={body.template}, {len(out)} bytes")
    return Response(
        content=out,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{body.template}-filled.pdf"'
            )
        },
    )
