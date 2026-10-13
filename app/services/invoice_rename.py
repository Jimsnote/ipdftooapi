"""发票批量重命名服务（二期工具三）。

设计依据：docs/2026-09-30_100232-invoice-batch-rename-design.md
- 命名规则：{前缀}{发票号码}_{购买方名称}_{开票日期}[_{价税合计}元].{原扩展名}
- 文件名安全清洗（§4.2）：票面字段是攻击者可控内容，拼名前必须过
  _sanitize_component()（非法字符/路径穿越/Windows 保留名/长度截断）；
- 去重（§4.3）：同批目标名重复追加 __2/__3（红冲重开常见）；
- 未识别文件保留原名进 ZIP + skipped 清单（§4.4），单文件失败不 fail 整批。

run_batch()：批量重命名编排（2026-10-13 自 routers/invoice_rename.py 逐行搬移，
行为不变——P0 设计 §6.4 / D2 拍板；网站路由与 skill 付费网关共用同一份编排，
同源纪律：两份逻辑日后漂移 = 事故源）。请求局部状态、无模块级可变状态。
"""
import os
import re
import zipfile
from typing import Dict, List, Optional, Tuple

from fastapi import HTTPException, UploadFile

from app.core.file_security import make_task_dir, safe_join
from app.core.logger import get_logger
from app.models.invoice_extract import (
    InvoiceRecord,
    RenameBatchResponse,
    RenameSkippedItem,
)
from app.services.invoice_extract import InvoiceExtractError, extract_bytes
from app.services.invoice_extract.extractor import SUPPORTED_EXTENSIONS
from app.services.ofd_validator import (
    OFD_MSG_ENCRYPTED,
    OFD_MSG_NOT_OFD,
    OfdEncryptedError,
    OfdFileError,
    validate_ofd_zip,
)

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# 约束（设计文档 §4.1/§4.2）
# ---------------------------------------------------------------------------

MAX_PREFIX_LEN = 50
MAX_BUYER_LEN = 30
MAX_FILENAME_LEN = 100

# Windows 保留名（大小写不敏感，含无扩展名与带扩展名两种形态基名）
_WIN_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# 非法字符：Windows 禁用字符 + 全部 ASCII 控制字符（含 \x00）
_ILLEGAL_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# 审查①-S2：Unicode 隐形/双向控制字符（零宽空格、RTLO 等）——
# 可在资源管理器中视觉伪装文件名（RTLO 反转显示），拼名前剥离
_INVISIBLE_CHARS_RE = re.compile(
    r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)


# ---------------------------------------------------------------------------
# §4.2 文件名安全清洗
# ---------------------------------------------------------------------------

def _is_win_reserved(name: str) -> bool:
    """Windows 保留名判断按基名（首个点号前）匹配：CON 与 CON.pdf 都保留。"""
    return name.split(".")[0].upper() in _WIN_RESERVED


def _sanitize_component(text: Optional[str], max_len: int) -> str:
    """把不可信文本清洗为安全的文件名组件。

    顺序：非法字符替换 → 去路径穿越点段 → 去首尾点/空格 → Windows
    保留名加前缀 → 截断 → 截断后再次兜底清洗（截断可能重新引入尾部
    点号或拼出保留名）。
    """
    if not text:
        return ""
    t = _INVISIBLE_CHARS_RE.sub("", text)
    t = _ILLEGAL_CHARS_RE.sub("_", t)
    t = t.replace("..", "_")
    t = t.strip(". ")
    if not t:
        return "_"
    if _is_win_reserved(t):
        t = "_" + t
    t = t[:max_len]
    t = t.strip(". ")
    if not t:
        return "_"
    if _is_win_reserved(t):
        t = "_" + t
    return t


def _split_ext(filename: str) -> Tuple[str, str]:
    """返回 (主名, 含点扩展名小写)；无扩展名 ext 为 ''。"""
    stem, dot, ext = filename.rpartition(".")
    if not dot:
        return filename, ""
    return stem, "." + ext.lower()


# ---------------------------------------------------------------------------
# §4.1 命名拼装
# ---------------------------------------------------------------------------

def _type_short_label(invoice_type: Optional[str]) -> str:
    """票种短标签：专票/普票；无法判定的类型返回空（不加段，宁缺勿错）。"""
    if not invoice_type:
        return ""
    if "专用发票" in invoice_type:
        return "专票"
    if "普通发票" in invoice_type:
        return "普票"
    return ""


def build_target_name(
    record: InvoiceRecord,
    ext: str,
    prefix: str = "",
    include_amount: bool = False,
    include_type: bool = False,
) -> str:
    """按 §4.1 规则生成目标文件名（已清洗，不含去重序号）。

    ext：原始扩展名（含点、小写），如 ".pdf"；保持上传格式不变更。
    前置条件：record.invoice_number 非空（调用方保证，空号码走 skipped）。
    票种段（2026-10-05 用户新增）：include_type 开启且类型可判定时，
    在日期后追加 _专票/_普票；金额开关语义保持"末尾附加价税合计"。
    """
    safe_prefix = _sanitize_component(prefix, MAX_PREFIX_LEN)
    number = _sanitize_component(record.invoice_number, 20) or "unknown"
    buyer = _sanitize_component(record.buyer_name, MAX_BUYER_LEN) or "unknown"
    date = _sanitize_component(record.issue_date, 10)  # YYYY-MM-DD 归一化产物

    safe_ext = ext if ext.startswith(".") else f".{ext}"
    parts = [number, buyer]
    if date:
        parts.append(date)
    if include_type:
        label = _type_short_label(record.invoice_type)
        if label:
            parts.append(label)
    if include_amount and record.total_with_tax:
        amount = _sanitize_component(record.total_with_tax, 20)
        if amount:
            parts.append(f"{amount}元")
    name = "_".join(parts) + safe_ext

    # 总长 ≤100：超限先截前缀、再截名称主体（扩展名永不截）
    if len(safe_prefix) + len(name) > MAX_FILENAME_LEN:
        safe_prefix = safe_prefix[: MAX_FILENAME_LEN - len(name)].strip(". ")
    if len(name) > MAX_FILENAME_LEN:
        stem, dot, e = name.rpartition(".")
        keep = MAX_FILENAME_LEN - len(e) - (1 if dot else 0)
        # 审查②：截断边界可落在点号上（尾部点/keep=0 → ".pdf" 隐藏名），补 strip
        stem = stem[:keep].strip(". ") if keep > 0 else ""
        name = (stem + (e if dot else "")) if dot else stem
        if not name.strip(". "):
            name = "_" + (e if dot else "")
    return safe_prefix + name


# ---------------------------------------------------------------------------
# §4.3 去重
# ---------------------------------------------------------------------------

def dedup_names(names: List[str]) -> Tuple[List[str], int]:
    """同批目标名重复时在扩展名前追加 __2、__3…（红冲重开场景）。

    返回 (最终名单, 解决的重复数)。不同扩展名的同名不算重名（整名比较）。
    极端情况：原名本身以 __2 结尾再撞名时序号继续递增。
    """
    seen: Dict[str, int] = {}
    used = set(names)
    out: List[str] = []
    resolved = 0
    for name in names:
        if name not in seen:
            seen[name] = 1
            out.append(name)
            continue
        seen[name] += 1
        resolved += 1
        stem, dot, ext = name.rpartition(".")
        serial = seen[name]
        candidate = f"{stem}__{serial}.{ext}" if dot else f"{name}__{serial}"
        while candidate in used:
            serial += 1
            candidate = f"{stem}__{serial}.{ext}" if dot else f"{name}__{serial}"
        used.add(candidate)
        out.append(candidate)
    return out, resolved


# ---------------------------------------------------------------------------
# §4.5 ZIP 打包（含报告）
# ---------------------------------------------------------------------------

def build_report_text(
    renamed_pairs: List[Tuple[str, str]],
    skipped: List[Tuple[str, str]],
) -> str:
    """生成 _重命名报告.txt 内容（成功/跳过清单与原因）。"""
    lines = [
        "发票批量重命名报告",
        f"成功重命名：{len(renamed_pairs)} 个；未识别（保留原名）：{len(skipped)} 个",
        "",
        "── 重命名明细 ──",
    ]
    if renamed_pairs:
        for orig, new in renamed_pairs:
            lines.append(f"{orig} → {new}")
    else:
        lines.append("（无）")
    lines.append("")
    lines.append("── 未识别文件 ──")
    if skipped:
        for name, reason in skipped:
            lines.append(f"{name}：{reason}")
        lines.append("")
        lines.append("提示：未识别的文件已按原文件名放入本压缩包。")
        lines.append("本工具仅支持数电票（XML / OFD / 文字层 PDF）；扫描件与旧版税控票暂不支持。")
    else:
        lines.append("（无）")
    return "\n".join(lines) + "\n"


def build_rename_zip(
    zip_path: str,
    renamed_entries: List[Tuple[str, str]],
    skipped_entries: List[Tuple[str, str]],
    renamed_pairs: List[Tuple[str, str]],
    skipped_pairs: List[Tuple[str, str]],
) -> None:
    """把重命名产物与未识别原文件打包；附 UTF-8 BOM 报告。

    renamed_entries / skipped_entries：[(磁盘路径, ZIP 内文件名)]；
    renamed_pairs：[(原文件名, 新文件名)]、skipped_pairs：[(原文件名, 原因)]
    —— 两者仅供报告文本使用。
    """
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for src, arcname in renamed_entries:
            zf.write(src, arcname)
        for src, arcname in skipped_entries:
            zf.write(src, arcname)
        report = build_report_text(renamed_pairs, skipped_pairs)
        zf.writestr("_重命名报告.txt", report.encode("utf-8-sig"))
    logger.info(
        f"rename zip built: {zip_path} renamed={len(renamed_entries)} "
        f"skipped={len(skipped_entries)}"
    )


# ---------------------------------------------------------------------------
# 批量重命名编排（2026-10-13 自 routers/invoice_rename.py 逐行搬移，行为不变；
# 网站路由与 skill 付费网关（skill_gateway）共用本入口——同源纪律）
# ---------------------------------------------------------------------------

TEMP_DIR = os.path.abspath("/app/temp" if os.path.exists("/app/temp") else "./temp")
os.makedirs(TEMP_DIR, exist_ok=True)

MAX_FILES = 50
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB / 单文件，与发票台账对齐
# 审查①-S1：50×10MB 全量驻留内存可达 500MB（2核2G 服务器不可承受），
# 与 merge 系 #20 守卫对齐：总量 100MB 上限，声明值预检 + 实读累计双保险
MAX_TOTAL_SIZE = 100 * 1024 * 1024


def _read_limited(file: UploadFile) -> bytes:
    """读上传内容：10MB 上限 → 413、空文件 → 400（设计 §8.2 预期）。"""
    data = file.file.read(MAX_FILE_SIZE + 1)
    if len(data) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"单文件不能超过 {MAX_FILE_SIZE // (1024 * 1024)}MB",
        )
    if not data:
        raise HTTPException(
            status_code=400, detail=f"文件为空：{file.filename or '未命名'}"
        )
    return data


def _sniff_ext(filename: str) -> str:
    """返回小写扩展名（含点）；白名单外抛 InvoiceExtractError（进 skipped）。"""
    lower = (filename or "").lower()
    for candidate in SUPPORTED_EXTENSIONS:
        if lower.endswith(candidate):
            return candidate
    raise InvoiceExtractError("不支持的文件类型（仅支持数电票 .xml / .ofd / .pdf）")


_MAGIC_CHECKS = {
    ".xml": lambda d: d.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"<"),
    ".pdf": lambda d: d.startswith(b"%PDF-"),
    ".ofd": lambda d: d.startswith(b"PK"),
}
_MAGIC_MESSAGES = {
    ".xml": "内容不是 XML 文本",
    ".pdf": "PDF 文件头校验失败",
    ".ofd": "OFD 文件头校验失败（应为 ZIP 包）",
}


def _parse_one(
    task_dir: str, index: int, orig_name: str, data: bytes, prefix: str,
    include_amount: bool, include_type: bool,
) -> Tuple[str, bytes, str]:
    """解析单文件并生成目标名。返回 (目标名, 数据, 扩展名)。

    一切业务性失败（类型/损坏/版式不支持/加密/非数电票）抛 InvoiceExtractError。
    """
    ext = _sniff_ext(orig_name)

    # 文件头校验（与台账 _read_checked 同款）：扩展名伪装的文件早失败早提示
    if not _MAGIC_CHECKS[ext](data):
        raise InvoiceExtractError(_MAGIC_MESSAGES[ext])

    # 原件落盘（仅 OFD：validate_ofd_zip 预扫描需要路径；XML/PDF 走内存字节流，
    # 审查①-S1：省去无谓落盘，任务目录磁盘占用减半）
    if ext == ".ofd":
        src_path = safe_join(task_dir, f"src_{index}{ext}")
        with open(src_path, "wb") as fh:
            fh.write(data)
        # §5.3：zip 炸弹/加密预扫描，与 /ofd/view、ofd-to-pdf 同款校验
        try:
            validate_ofd_zip(src_path)
        except OfdEncryptedError:
            raise InvoiceExtractError(OFD_MSG_ENCRYPTED)
        except OfdFileError:
            raise InvoiceExtractError(OFD_MSG_NOT_OFD)

    rec = extract_bytes(data, orig_name, ext)
    if not rec.invoice_number:
        raise InvoiceExtractError(
            "未能识别出发票号码——可能不是数电票标准版式，暂不支持"
        )
    target = build_target_name(rec, ext, prefix, include_amount, include_type)
    return target, data, ext


def run_batch(
    files: List[UploadFile],
    prefix: str = "",
    include_amount: bool = False,
    include_type: bool = False,
) -> RenameBatchResponse:
    """批量重命名编排（读限/预检/逐文件解析/skip 归置/ZIP 装配）。

    自 routers/invoice_rename.py::rename_batch 逐行搬移（§6.4，行为不变）；
    现有 pytest 基线照跑即安全网。
    """
    if not files:
        raise HTTPException(status_code=400, detail="请至少上传一个文件")
    if len(files) > MAX_FILES:
        raise HTTPException(status_code=400, detail=f"一次最多上传 {MAX_FILES} 个文件")
    if prefix and len(prefix) > MAX_PREFIX_LEN:
        raise HTTPException(status_code=400, detail=f"前缀最长 {MAX_PREFIX_LEN} 个字符")
    # 审查①-S1：声明值预检（快速失败；size 可为 None 按 0 计，后面实读累计兜底）
    if sum(f.size or 0 for f in files) > MAX_TOTAL_SIZE:
        raise HTTPException(
            status_code=400, detail="所有文件加起来不能超过 100MB，请分批处理"
        )

    task_id, task_dir = make_task_dir(TEMP_DIR)

    # 请求局部状态（并发安全）
    skipped_pairs: List[Tuple[str, str]] = []  # (原文件名, 原因)
    skipped_files: List[Tuple[bytes, str]] = []  # (数据, 原文件名) → 原名进 ZIP
    renamed: List[Tuple[str, str, bytes, str]] = []  # (原文件名, 目标名, 数据, 扩展名)
    total_read = 0  # 审查①-S1：实读字节累计（防声明值伪造）

    for index, f in enumerate(files):
        orig_name = f.filename or f"file_{index + 1}"
        data = b""
        try:
            data = _read_limited(f)
            total_read += len(data)
            if total_read > MAX_TOTAL_SIZE:
                raise HTTPException(
                    status_code=400,
                    detail="所有文件加起来不能超过 100MB，请分批处理",
                )
            target, data, ext = _parse_one(
                task_dir, index, orig_name, data, prefix, include_amount,
                include_type,
            )
            renamed.append((orig_name, target, data, ext))
        except InvoiceExtractError as e:
            # §4.4：单文件失败不 fail 整批，保留原名进 ZIP
            skipped_pairs.append((orig_name, str(e)))
            skipped_files.append((data, orig_name))
        except HTTPException:
            raise  # 413/400 等参数类错误整批返回
        except Exception as e:  # noqa: BLE001
            logger.error(f"rename parse failed for {orig_name}: {e}")
            skipped_pairs.append((orig_name, "解析失败，请确认文件未损坏"))
            skipped_files.append((data, orig_name))

    # §4.3 去重（仅重命名成功的文件；未识别文件保留原名不参与）
    targets, duplicates_resolved = dedup_names([t for _, t, _, _ in renamed])

    renamed_entries: List[Tuple[str, str]] = []
    renamed_report: List[Tuple[str, str]] = []
    for i, ((orig, target, data, ext), final) in enumerate(zip(renamed, targets)):
        final_path = safe_join(task_dir, f"out_{i}{ext}")
        with open(final_path, "wb") as fh:
            fh.write(data)
        renamed_entries.append((final_path, final))
        renamed_report.append((orig, final))

    skipped_entries: List[Tuple[str, str]] = []
    # 审查①-S3：ZIP 允许重复条目名（解压时后者覆盖前者 → 静默丢文件）。
    # skipped arcname 须对「报告名 + renamed 终名 + 先前 skipped」全量查重
    used_arcnames = {"_重命名报告.txt"} | set(targets)
    for i, (data, orig_name) in enumerate(skipped_files):
        # 原文件名也是不可信输入：进 ZIP 前必须清洗（防路径穿越/非法字符）
        arcname = _sanitize_component(orig_name, MAX_FILENAME_LEN)
        if not arcname or arcname == "_":
            arcname = f"unrecognized_{i}.bin"
        if arcname in used_arcnames:
            stem, dot, ext = arcname.rpartition(".")
            serial = 2
            candidate = f"{stem}__{serial}.{ext}" if dot else f"{arcname}__{serial}"
            while candidate in used_arcnames:
                serial += 1
                candidate = f"{stem}__{serial}.{ext}" if dot else f"{arcname}__{serial}"
            arcname = candidate
        used_arcnames.add(arcname)
        path = safe_join(task_dir, f"skip_{i}.bin")
        with open(path, "wb") as fh:
            fh.write(data)
        skipped_entries.append((path, arcname))

    zip_path = safe_join(task_dir, f"invoice_renamed_{task_id[:8]}.zip")
    build_rename_zip(
        zip_path, renamed_entries, skipped_entries, renamed_report, skipped_pairs
    )

    total = len(files)
    logger.info(
        f"invoice rename task {task_id}: total={total} renamed={len(renamed)} "
        f"skipped={len(skipped_pairs)} duplicates_resolved={duplicates_resolved}"
    )
    return RenameBatchResponse(
        task_id=task_id,
        download_url=f"/api/v1/invoice/rename-download/{task_id}",
        total=total,
        renamed=len(renamed),
        duplicates_resolved=duplicates_resolved,
        skipped=[RenameSkippedItem(file=n, reason=r) for n, r in skipped_pairs],
    )
