"""invoice-toolkit-pro 统一付费网关（支付宝 AI 按量付费 / A402）。

POST /api/v1/skill/invoice —— 单端点单服务注册（一次注册 = 一个 URL + 一个单价）：
- multipart/form-data → op = merge | extract | rename（文件批）
- application/json    → op = dedup（查重键批，服务端零持久化票面数据）

支付门禁（A402 协议）：
- 无 Payment-Proof → INSERT created + 402 + Payment-Needed（Base64URL 账单）
- 有 Payment-Proof → alipay verify（五项业务校验）→ **验后**回放缓存
  （review② F1：回放必须先过 verify——Payment-Proof 头只是 Base64 JSON，
  自述 trade_no 无密码学保证，验前回放=白嫖向量）→ 原子抢占（R2）→ 执行业务
  → mark_fulfilled + result_cache → 异步履约回执 → 200
- 并发重入 → 409「处理中」（result_cache 已有则直接回放）
- 业务执行异常 → release_claim（订单回 created，买家可携凭证重试，不卡死 409）

GET /api/v1/skill/invoice/download/{task_id} —— 产物下载（不设二次付费；
temp 实际清理窗口约 12~36 小时，对用户文案承诺「约 24 小时」）。

设计依据：docs/2026-10-13_invoice-toolkit-pro-p0-design.md §2/§6/§7 + review① R1-R4。
"""
import json
import os
import threading
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.config import settings
from app.core.file_security import (
    get_task_dir,
    make_task_dir,
    safe_join,
    save_upload_file,
)
from app.core.logger import get_logger
from app.models.invoice_extract import ExportRowRequest
from app.services.invoice_dedup import (
    LedgerEntry,
    ParsedTicket,
    dedup_tickets,
    make_key,
    to_ledger_entries,
)
from app.services.invoice_extract import InvoiceExtractError, extract_bytes
from app.services.invoice_extract.extractor import SUPPORTED_EXTENSIONS
from app.services.invoice_extract.exporter import build_filename, export_xlsx
from app.services.invoice_merge_shared import convert_ofd_batch_async
from app.services.invoice_merger import InvoiceMerger
from app.services.invoice_rename import run_batch as rename_run_batch
from app.services.skillpay import (
    AlipayClient,
    AlipayGatewayError,
    OrderStore,
    PaymentProofError,
    build_bill,
    decode_payment_proof,
    encode_payment_needed,
    generate_out_trade_no,
    generate_resource_id,
    goods_name_for,
    skillpay_config_from_settings,
)

router = APIRouter()
logger = get_logger(__name__)

TEMP_DIR = os.path.abspath("/app/temp" if os.path.exists("/app/temp") else "./temp")
os.makedirs(TEMP_DIR, exist_ok=True)

DATA_DIR = os.path.abspath("/app/data" if os.path.exists("/app/data") else "./data")
ORDER_DB_PATH = os.path.join(DATA_DIR, "skillpay_orders.db")

# ---------------------------------------------------------------------------
# 限额（与网站工具同款守卫；P0 §2.1/§6.5——D1：对外文案「单批最多 50 张」）
# ---------------------------------------------------------------------------

MAX_FILES = 50
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB / 单文件
MAX_TOTAL_SIZE = 100 * 1024 * 1024  # 100MB / 批（声明值预检 + 实读累计双保险）

MAX_TICKETS = 200  # dedup 键批
MAX_LEDGER = 50000  # 对齐网站版 ledger.ts 守卫（review① R4）
MAX_JSON_BODY = 2 * 1024 * 1024  # tickets+ledger JSON ≤ 2MB

FILE_OPS = ("merge", "extract", "rename")
OP_EXT_ALLOWLIST = {
    "merge": (".pdf", ".ofd"),  # XML 发票暂不支持合并（能力边界）
    "extract": (".xml", ".ofd", ".pdf"),
    "rename": (".xml", ".ofd", ".pdf"),
}

DOWNLOAD_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".zip": "application/zip",
}


# ---------------------------------------------------------------------------
# 请求模型（dedup JSON 模式，P0 §7.2）
# ---------------------------------------------------------------------------


class DedupTicketIn(BaseModel):
    fileRef: str = Field(..., max_length=300)
    number: Optional[str] = Field(None, max_length=64)
    code: Optional[str] = Field(None, max_length=32)
    amount: Optional[str] = Field(None, max_length=32)
    date: Optional[str] = Field(None, max_length=10)


class DedupLedgerIn(BaseModel):
    number: str = Field(..., max_length=64)
    code: Optional[str] = Field(None, max_length=32)
    amount: Optional[str] = Field(None, max_length=32)
    firstName: str = Field("", max_length=300)
    firstDay: str = Field("", max_length=10)


class DedupRequest(BaseModel):
    op: str = "dedup"
    tickets: List[DedupTicketIn] = Field(..., min_length=1, max_length=MAX_TICKETS)
    ledger: List[DedupLedgerIn] = Field(default_factory=list, max_length=MAX_LEDGER)


class FilesPayload:
    """multipart 文件批的解析产物（预检通过、待支付门禁）。"""

    def __init__(self, op: str, files: List[UploadFile], prefix: str = "",
                 include_amount: bool = False, include_type: bool = False):
        self.op = op
        self.files = files
        self.prefix = prefix
        self.include_amount = include_amount
        self.include_type = include_type

    @property
    def file_count(self) -> int:
        return len(self.files)


class JsonPayload:
    def __init__(self, tickets: List[DedupTicketIn], ledger: List[DedupLedgerIn]):
        self.tickets = tickets
        self.ledger = ledger

    @property
    def op(self) -> str:
        return "dedup"

    @property
    def file_count(self) -> int:
        return len(self.tickets)


# ---------------------------------------------------------------------------
# 单例（懒加载 + 锁；测试通过 _reset_singletons() 复位后注入自己的实例）
# ---------------------------------------------------------------------------

_store_lock = threading.Lock()
_store: Optional[OrderStore] = None

_client_lock = threading.Lock()
_client: Optional[AlipayClient] = None


def _get_order_store() -> OrderStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = OrderStore(ORDER_DB_PATH)
        return _store


def _get_alipay_client() -> AlipayClient:
    global _client
    with _client_lock:
        if _client is None:
            _client = AlipayClient(skillpay_config_from_settings(settings))
        return _client


def _reset_singletons() -> None:
    """仅测试用：复位模块级单例（订单库路径 / 支付客户端注入前调用）。"""
    global _store, _client
    with _store_lock:
        _store = None
    with _client_lock:
        _client = None


# ---------------------------------------------------------------------------
# 请求解析（双 Content-Type 分发，P0 §2.1）与预检
# ---------------------------------------------------------------------------


def _sniff_magic(f: UploadFile, suffix: str) -> None:
    """文件头嗅探（16B 读后回卷）：扩展名伪装的文件在付费前就挡掉。"""
    header = f.file.read(16)
    f.file.seek(0)
    if suffix == ".pdf":
        ok = header.startswith(b"%PDF-")
    elif suffix == ".ofd":
        ok = header.startswith(b"PK")
    elif suffix == ".xml":
        ok = header.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<")
    else:  # pragma: no cover - 扩展名已在白名单层拦截
        ok = False
    if not ok:
        raise HTTPException(
            status_code=400,
            detail=f"文件头校验失败：{f.filename or '未命名'}（与扩展名不符）",
        )


async def _parse_files_request(request: Request) -> FilesPayload:
    form = await request.form()
    op = form.get("op")
    if op not in FILE_OPS:
        raise HTTPException(status_code=400, detail="op 无效（支持 merge/extract/rename）")

    files: List[UploadFile] = []
    for key, value in form.multi_items():
        if key == "files":
            # request.form() 产出 starlette 原生 UploadFile（fastapi 版是其子类，
            # 反向 isinstance 不成立——必须对 starlette 基类判定）
            if not isinstance(value, StarletteUploadFile):
                raise HTTPException(status_code=400, detail="files 字段必须是文件")
            files.append(value)

    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一个文件")
    if len(files) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"单批最多 {MAX_FILES} 个文件，请分批处理")

    allow = OP_EXT_ALLOWLIST[op]
    # review② F3：实测 spool 尺寸（声明值 f.size 可伪造/缺失，chunked 上传时为
    # None）——seek/tell 量出的就是已接收的真实字节数；与 rename/extract 的
    # 实读累计同口径，merge 不再单靠声明值（50×10MB 全落盘的磁盘瞬时压力）。
    actual_total = 0
    for f in files:
        suffix = Path(f.filename or "").suffix.lower()
        if suffix not in allow:
            if suffix == ".xml":
                raise HTTPException(
                    status_code=400, detail="XML 发票暂不支持合并，支持提取/重命名/查重"
                )
            raise HTTPException(
                status_code=400, detail="仅支持数电票 .xml / .ofd / .pdf 文件"
            )
        f.file.seek(0, os.SEEK_END)
        actual_size = f.file.tell()
        f.file.seek(0)
        if actual_size > MAX_FILE_SIZE:
            raise HTTPException(status_code=413, detail="单文件不能超过 10MB")
        actual_total += actual_size
        _sniff_magic(f, suffix)
    if actual_total > MAX_TOTAL_SIZE:
        raise HTTPException(status_code=400, detail="所有文件加起来不能超过 100MB，请分批处理")

    prefix = str(form.get("prefix") or "")
    include_amount = str(form.get("include_amount") or "").lower() in ("true", "1", "on")
    include_type = str(form.get("include_type") or "").lower() in ("true", "1", "on")
    return FilesPayload(op=op, files=files, prefix=prefix,
                        include_amount=include_amount, include_type=include_type)


async def _read_bounded_json_body(request: Request, limit: int) -> dict:
    """流式限量读 JSON body（review② F2）。

    content-length 头可被 chunked 传输绕过（无此头），request.json()/body()
    会无上限读完整个流——2C2G 上的 OOM 向量。此处逐 chunk 累计，超限即 413，
    内存上界 = limit。
    """
    content_length = request.headers.get("content-length")
    try:
        if content_length and int(content_length) > limit:
            raise HTTPException(
                status_code=413, detail=f"查重请求体过大（上限 {limit // (1024 * 1024)}MB）"
            )
    except ValueError:  # header 异常值放行，交给流式累计兜底
        pass

    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status_code=413, detail=f"查重请求体过大（上限 {limit // (1024 * 1024)}MB）"
            )
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON") from e


async def _parse_json_request(request: Request) -> JsonPayload:
    body = await _read_bounded_json_body(request, MAX_JSON_BODY)
    try:
        req = DedupRequest.model_validate(body)
    except Exception as e:  # noqa: BLE001 - 全局 422 处理器不覆盖手写解析路径
        raise HTTPException(status_code=400, detail=f"查重请求参数无效：{e}") from e
    if (req.op or "dedup") != "dedup":
        raise HTTPException(status_code=400, detail="JSON 模式仅支持 op=dedup")
    return JsonPayload(tickets=req.tickets, ledger=req.ledger)


# ---------------------------------------------------------------------------
# 402 / 回放 / 409 响应构造
# ---------------------------------------------------------------------------


async def _new_bill_response(
    store: OrderStore,
    payload,
    *,
    error: str = "Payment Needed",
    message: Optional[str] = None,
    gateway_code: Optional[str] = None,
) -> JSONResponse:
    """无有效凭证：新建订单 + 出账单（402 + Payment-Needed 头，P0 §2.2 A/C）。"""
    cfg = skillpay_config_from_settings(settings)
    out_trade_no = generate_out_trade_no()
    resource_id = generate_resource_id(payload.op)
    store.create_order(out_trade_no, payload.op, resource_id, cfg.amount, payload.file_count)
    bill = build_bill(cfg, op=payload.op, file_count=payload.file_count,
                      out_trade_no=out_trade_no, resource_id=resource_id)

    if message is None:
        message = (
            f"请支付 {cfg.amount} {bill['protocol']['currency']} 以处理本批发票"
            f"（{cfg.pay_before_minutes} 分钟内有效）"
        )
    body = {
        "error": error,
        "message": message,
        "out_trade_no": out_trade_no,
        "resource_id": resource_id,
        "op": payload.op,
        "amount": cfg.amount,
        "currency": bill["protocol"]["currency"],
        "pay_before": bill["protocol"]["pay_before"],
    }
    if gateway_code:
        body["gateway_code"] = gateway_code
    logger.info(
        "skillpay bill issued: op=%s files=%s out_trade_no=%s amount=%s code=%s",
        payload.op, payload.file_count, out_trade_no, cfg.amount, gateway_code or "-",
    )
    return JSONResponse(
        status_code=402,
        content=body,
        headers={"Payment-Needed": encode_payment_needed(bill)},
    )


def _replay_response(order) -> JSONResponse:
    """幂等重发（规则 4）：回放缓存 + already_fulfilled=true，不重处理不重扣。"""
    body = order.result_body()
    if body is None:  # pragma: no cover - fulfilled 必有缓存（防御分支）
        body = {"error": "Conflict", "message": "订单状态异常，请稍后重试"}
    body = dict(body)
    body["already_fulfilled"] = True
    return JSONResponse(status_code=200, content=body)


# ---------------------------------------------------------------------------
# 业务执行（四 op 编排，P0 §6/§7——全部复用现有服务）
# ---------------------------------------------------------------------------


async def _run_merge(files: List[UploadFile]) -> dict:
    """§6.1：PDF/OFD 混合 → A4 合并 PDF（网站 analyze/merge 双阶段的一句话版）。

    上传循环镜像 app/routers/invoice.py::analyze_invoices（input_NNN.ofd /
    invoice_NNN.pdf 命名空间与 OFD 批量转换回填逻辑同款，零改动复用）。
    """
    task_id, task_dir = make_task_dir(TEMP_DIR)

    saved_paths: List[Optional[str]] = []
    original_names: List[str] = []
    ofd_paths = []  # (index, path) 待转换 OFD
    for index, f in enumerate(files):
        name = (f.filename or "").lower()
        if name.endswith(".ofd"):
            path = save_upload_file(
                f, safe_join(task_dir, f"input_{index + 1:03d}.ofd"),
                MAX_FILE_SIZE, (".ofd",),
            )
            ofd_paths.append((index, path))
            saved_paths.append(None)
        else:
            path = save_upload_file(
                f, safe_join(task_dir, f"invoice_{index + 1:03d}.pdf"),
                MAX_FILE_SIZE, (".pdf",),
            )
            saved_paths.append(path)
        original_names.append(f.filename or f"invoice_{index + 1:03d}.pdf")

    if ofd_paths:
        await convert_ofd_batch_async(
            task_dir, ofd_paths, [original_names[i] for i, _ in ofd_paths]
        )
        for idx, _ in ofd_paths:
            saved_paths[idx] = safe_join(task_dir, f"invoice_{idx + 1:03d}.pdf")

    pdf_paths = [p for p in saved_paths if p]
    missing = [p for p in pdf_paths if not os.path.exists(p)]
    if missing:
        logger.error("skill merge task %s: missing converted files: %s", task_id, missing)
        raise HTTPException(status_code=500, detail="发票转换结果异常，请重新发起")

    merger = InvoiceMerger()
    await run_in_threadpool(merger.analyze, pdf_paths)
    output_path = safe_join(task_dir, "merged_invoices.pdf")
    info = await run_in_threadpool(
        merger.merge,
        output_path=output_path,
        per_page=4, margin="standard", crop_marks=True,
        page_numbers=True, layout="stacked", binding_mm=0.0,
    )
    logger.info(
        "skill merge task %s completed: %s invoices -> %s pages",
        task_id, info["invoices_count"], info["page_count"],
    )
    return {
        "op": "merge",
        "task_id": task_id,
        "download_url": f"/api/v1/skill/invoice/download/{task_id}",
        "message": f"已合并 {info['invoices_count']} 张发票为 {info['page_count']} 页 A4 PDF",
    }


def _run_extract(files: List[UploadFile]) -> dict:
    """§6.2：三格式 → Excel 台账（内存态编排，逐文件失败隔离）。"""
    task_id, task_dir = make_task_dir(TEMP_DIR)

    records = []
    failures = []
    total_read = 0
    for index, f in enumerate(files):
        orig_name = f.filename or f"file_{index + 1}"
        data = f.file.read(MAX_FILE_SIZE + 1)
        if len(data) > MAX_FILE_SIZE:
            raise HTTPException(status_code=413, detail="单文件不能超过 10MB")
        if not data:
            failures.append({"file": orig_name, "reason": "文件为空"})
            continue
        total_read += len(data)
        if total_read > MAX_TOTAL_SIZE:
            raise HTTPException(status_code=400, detail="所有文件加起来不能超过 100MB，请分批处理")
        lower = (orig_name or "").lower()
        ext = next((c for c in SUPPORTED_EXTENSIONS if lower.endswith(c)), None)
        if ext is None:
            failures.append({"file": orig_name, "reason": "不支持的文件类型"})
            continue
        try:
            records.append(extract_bytes(data, orig_name, ext))
        except InvoiceExtractError as e:
            failures.append({"file": orig_name, "reason": str(e)})
        except Exception as e:  # noqa: BLE001
            logger.error("skill extract parse failed for %s: %r", orig_name, e)
            failures.append({"file": orig_name, "reason": "解析失败，请确认文件未损坏"})

    rows = [ExportRowRequest(**rec.model_dump()) for rec in records]
    xlsx_bytes = export_xlsx(rows)
    out_path = safe_join(task_dir, build_filename("xlsx"))
    with open(out_path, "wb") as fh:
        fh.write(xlsx_bytes)
    return {
        "op": "extract",
        "task_id": task_id,
        "download_url": f"/api/v1/skill/invoice/download/{task_id}",
        "message": f"已提取 {len(records)} 个发票记录为 Excel 台账"
                   f"（失败 {len(failures)} 个）",
        "success_count": len(records),
        "failure_count": len(failures),
        "failures": failures,
    }


def _run_dedup(tickets: List[DedupTicketIn], ledger: List[DedupLedgerIn]) -> dict:
    """§7.2：查重键批（纯计算零存储；键归一化与判重全在服务端）。"""
    parsed = [
        ParsedTicket(
            file_name=t.fileRef, file_ref=t.fileRef,
            code=t.code, number=t.number, amount=t.amount, date=t.date,
        )
        for t in tickets
    ]
    ledger_map = {}
    for e in ledger:
        key = make_key(e.number, e.code)
        if key and key not in ledger_map:
            ledger_map[key] = LedgerEntry(
                key=key, number=e.number, code=e.code, amount=e.amount,
                first_name=e.firstName, first_day=e.firstDay,
            )
    rows = dedup_tickets(parsed, ledger_map)
    entries = to_ledger_entries(rows)

    summary = {"total": len(rows), "ok": 0, "duplicate": 0,
               "suspicious": 0, "unrecognized": 0}
    for r in rows:
        summary[r.status] = summary.get(r.status, 0) + 1

    return {
        "op": "dedup",
        "rows": [
            {
                "fileRef": r.file_ref, "number": r.number, "code": r.code,
                "amount": r.amount, "date": r.date,
                "key": r.key, "status": r.status, "note": r.note,
            }
            for r in rows
        ],
        "ledgerEntries": [
            {
                "key": e.key, "number": e.number, "code": e.code,
                "amount": e.amount, "firstName": e.first_name, "firstDay": e.first_day,
            }
            for e in entries
        ],
        "summary": summary,
    }


async def _dispatch(op: str, payload) -> dict:
    if op == "merge":
        return await _run_merge(payload.files)
    if op == "extract":
        return await run_in_threadpool(_run_extract, payload.files)
    if op == "rename":
        response = await run_in_threadpool(
            rename_run_batch, payload.files, payload.prefix,
            payload.include_amount, payload.include_type,
        )
        body = response.model_dump()
        body["op"] = "rename"  # §2.2 B：op 结果统一携带 op 字段
        return body
    if op == "dedup":
        return await run_in_threadpool(_run_dedup, payload.tickets, payload.ledger)
    raise HTTPException(status_code=400, detail=f"未知 op：{op}")  # pragma: no cover


def _safe_fulfillment_confirm(trade_no: str) -> None:
    try:
        _get_alipay_client().fulfillment_confirm(trade_no)
    except Exception as e:  # noqa: BLE001 - 回执失败仅记日志（P0 §4 规则 3）
        logger.warning("fulfillment confirm crashed: trade_no=%s err=%r", trade_no, e)


# ---------------------------------------------------------------------------
# 主端点
# ---------------------------------------------------------------------------


@router.post("/invoice", summary="invoice-toolkit-pro 付费网关（AI 按量付费）")
async def skill_invoice(request: Request, background_tasks: BackgroundTasks):
    content_type = (request.headers.get("content-type") or "").lower()

    store = _get_order_store()
    proof_header = request.headers.get("payment-proof")
    proof_error: Optional[str] = None
    payment_proof = trade_no = client_session = None
    if proof_header:
        try:
            payment_proof, trade_no, client_session = decode_payment_proof(proof_header)
        except PaymentProofError as e:
            proof_error = str(e)
        # review② F1：此处不做任何回放——decode 只是 Base64 解 JSON，
        # 自述 trade_no 无密码学保证（会出现在买家付款记录/日志/截图里，
        # 非高熵秘密）。回放统一在 verify 通过之后（下方「已履约回放」分支）。

    # 请求解析 + 预检（付费前挡掉非法输入，D1 守卫）
    if "application/json" in content_type:
        payload = await _parse_json_request(request)
    elif "multipart/form-data" in content_type:
        payload = await _parse_files_request(request)
    else:
        raise HTTPException(
            status_code=400, detail="Content-Type 须为 multipart/form-data 或 application/json"
        )

    # ---- 支付门禁 ----
    if not proof_header:
        return await _new_bill_response(store, payload)
    if proof_error is not None:
        # Payment-Proof 解析失败：402 新账单（绝不 500，P0 §13 风险 4）
        return await _new_bill_response(
            store, payload, error="Payment Invalid", message=f"支付凭证无效：{proof_error}"
        )

    # 验凭证（网关调用放线程池；只读校验，重复调用无副作用不重扣钱）
    try:
        result = await run_in_threadpool(
            _get_alipay_client().verify_payment, payment_proof, trade_no, client_session
        )
    except AlipayGatewayError as e:
        logger.warning(
            "skillpay verify failed: trade_no=%s code=%s detail=%s",
            trade_no, e.sub_code, e.internal,
        )
        return await _new_bill_response(
            store, payload, error="Payment Invalid",
            message=e.user_message, gateway_code=e.sub_code,
        )

    # 五项业务校验（active/amount/out_trade_no/resource_id/未重复履约）
    order = store.get_order(result.out_trade_no) if result.out_trade_no else None
    if (
        order is None
        or not result.active
        or result.resource_id != order.resource_id
        or result.amount != order.amount
        or payload.op != order.op
    ):
        logger.warning(
            "skillpay business check failed: trade_no=%s out_trade_no=%s active=%s "
            "amount=%s/%s resource=%s op=%s/%s",
            trade_no, result.out_trade_no, result.active, result.amount,
            order.amount if order else "-", result.resource_id, payload.op,
            order.op if order else "-",
        )
        return await _new_bill_response(
            store, payload, error="Payment Invalid", message="支付凭证校验未通过，请重新支付"
        )

    # 已履约回放（规则 4 + review② F1 修复后的**唯一**回放入口）：
    # verify 已通过 → 回放缓存 + already_fulfilled，不重处理不重履约。
    if order.status == "fulfilled" and order.result_cache:
        logger.info(
            "skillpay verified replay: trade_no=%s out_trade_no=%s",
            trade_no, order.out_trade_no,
        )
        return _replay_response(order)

    # 原子抢占（review① R2：防 verified→fulfilled 之间并发重入双跑业务）
    if not store.claim_for_verify(order.out_trade_no, trade_no):
        current = store.get_order(order.out_trade_no)
        if current is not None and current.result_cache:
            return _replay_response(current)
        return JSONResponse(
            status_code=409,
            content={
                "error": "Processing",
                "message": "订单正在处理中，请稍后重试",
                "out_trade_no": order.out_trade_no,
            },
        )

    # ---- 执行业务 ----
    try:
        result_body = await _dispatch(order.op, payload)
    except Exception:
        # 业务失败不把订单卡死在 verified（释放后买家携同一凭证可重试）
        store.release_claim(order.out_trade_no)
        logger.exception(
            "skillpay dispatch failed: op=%s out_trade_no=%s trade_no=%s",
            order.op, order.out_trade_no, trade_no,
        )
        raise

    body = dict(result_body)
    body["out_trade_no"] = order.out_trade_no
    body["trade_no"] = trade_no
    body["already_fulfilled"] = False
    store.mark_fulfilled(
        order.out_trade_no, body.get("task_id"),
        json.dumps(body, ensure_ascii=False, separators=(",", ":")),
    )
    background_tasks.add_task(run_in_threadpool, _safe_fulfillment_confirm, trade_no)
    return JSONResponse(status_code=200, content=body)


@router.get("/invoice/download/{task_id}", summary="下载付费处理产物")
async def skill_download(task_id: str):
    """按 task_dir 内产物扩展名分发（merged PDF / 发票台账 xlsx / 重命名 ZIP）。

    不设二次付费——付费发生在处理端点；下载凭 task_id，与网站工具同策略。
    """
    task_dir = get_task_dir(TEMP_DIR, task_id)
    if not os.path.exists(task_dir):
        raise HTTPException(status_code=404, detail="任务不存在或已过期（约 24 小时）")

    candidates = [
        f for f in os.listdir(task_dir)
        if f.startswith(("merged_invoices", "发票台账_", "invoice_renamed_"))
    ]
    if not candidates:
        raise HTTPException(status_code=404, detail="输出文件不存在")
    path = safe_join(task_dir, sorted(candidates)[0])
    suffix = os.path.splitext(path)[1].lower()
    media_type = DOWNLOAD_MEDIA_TYPES.get(suffix, "application/octet-stream")
    return FileResponse(path, media_type=media_type, filename=os.path.basename(path))
