"""发票批量重命名服务（二期工具三）。

设计依据：docs/2026-09-30_100232-invoice-batch-rename-design.md
- 命名规则：{前缀}{发票号码}_{购买方名称}_{开票日期}[_{价税合计}元].{原扩展名}
- 文件名安全清洗（§4.2）：票面字段是攻击者可控内容，拼名前必须过
  _sanitize_component()（非法字符/路径穿越/Windows 保留名/长度截断）；
- 去重（§4.3）：同批目标名重复追加 __2/__3（红冲重开常见）；
- 未识别文件保留原名进 ZIP + skipped 清单（§4.4），单文件失败不 fail 整批。

核心逻辑全部纯函数，可脱离 FastAPI 单测。
"""
import re
import zipfile
from typing import Dict, List, Optional, Tuple

from app.core.logger import get_logger
from app.models.invoice_extract import InvoiceRecord

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
