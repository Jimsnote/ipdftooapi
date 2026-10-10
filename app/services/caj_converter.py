"""CAJ 转 PDF 服务（vendored caj2pdf 内核，GLWTPL 许可）。

设计依据：docs/CAJ_TO_PDF_DESIGN.md（2026-10-10 阶段 0 样本实测 6/6 通过）

CAJ 后缀内部的格式分层（实测 + caj2pdf 项目 Wiki）：
  - pdf-embedded：文件头即 %PDF——知网近年部分下载的"伪后缀"，改名即 PDF（质量完美）
  - CAJ / C8：caj2pdf 支持完整转换；C8 走 JBIG 解码输出图片型 PDF（视觉质量高，不可搜索）
  - HN：需 libjbigdec.so（部署时编译），部分支持
  - KDH / 未知：明确拒绝

转换以子进程调用 vendored caj2pdf CLI（类比 LibreOffice/Ghostscript 的
外部工具集成模式）：cwd 固定为 vendor 目录（JBIG so 相对加载），
解释器用 sys.executable（venv 内安装 PyPDF2==1.28.6 与主服务隔离）。
"""

import asyncio
import os
import shutil
import subprocess
from typing import Optional, Tuple

from app.core.logger import get_logger

logger = get_logger(__name__)

CAJ_EXTENSIONS = (".caj", ".kdh", ".hn")
MAX_CAJ_SIZE = 100 * 1024 * 1024  # 100MB（大 CAJ 的 JBIG 解码内存峰值未压测，
                                  # 首版保守对齐 iloveofd 免费档；压测后再放宽）
CONVERT_TIMEOUT_SECONDS = 120

# 并发限制（对抗自查：与 OFD 转换防护对齐，防止多个大文件解码内存峰值叠加）
_SEM = asyncio.Semaphore(1)

_VENDOR_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "vendor", "caj2pdf")
)
_CAJ_CLI = os.path.join(_VENDOR_DIR, "caj2pdf")


def detect_caj_type(path: str) -> str:
    """识别 CAJ 文件内部类型。

    返回：pdf-embedded / C8 / CAJ / HN / KDH / unknown
    """
    with open(path, "rb") as f:
        head = f.read(512)
    if head.startswith(b"%PDF"):
        return "pdf-embedded"
    if head[:1] == b"\xc8" or b"C8" in head[:12]:
        return "C8"
    if head[:3] == b"KDH" or b"KDH" in head[:20]:
        return "KDH"
    if head[:2] == b"HN" or b"HN" in head[:20]:
        return "HN"
    if head[:3] == b"CAJ" or b"CAJ" in head[:20]:
        return "CAJ"
    # 兜底：头部含 filetype 字段标记
    lowered = head.lower()
    if b"filetype" in lowered:
        idx = lowered.find(b"filetype")
        tail = head[idx + 8 : idx + 16].strip(b"\x00 \t\r\n")
        if tail:
            token = "".join(chr(c) for c in tail[:3] if 32 < c < 127)
            if token:
                return token
    return "unknown"


def _run_convert(input_path: str, output_path: str) -> Tuple[bool, str]:
    """同步执行 caj2pdf convert（在线程池中调用以避免阻塞事件循环）。"""
    env = dict(os.environ)
    # JBIG 解码 so 相对 cwd 加载（jbigdec.py 内 ctypes CDLL("./libjbigdec.so")）
    env["LD_LIBRARY_PATH"] = _VENDOR_DIR + os.pathsep + env.get("LD_LIBRARY_PATH", "")
    try:
        proc = subprocess.run(
            [
                "python3",
                _CAJ_CLI,
                "convert",
                input_path,
                "-o",
                output_path,
            ],
            cwd=_VENDOR_DIR,
            env=env,
            capture_output=True,
            text=True,
            timeout=CONVERT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, f"转换超时（超过 {CONVERT_TIMEOUT_SECONDS} 秒）"
    except OSError as e:
        return False, f"转换内核启动失败: {e}"
    if proc.returncode != 0:
        tail = (proc.stdout or "") + (proc.stderr or "")
        tail = tail.strip().splitlines()[-1] if tail.strip() else f"exit={proc.returncode}"
        return False, tail[:200]
    return True, "ok"


def _validate_output(output_path: str) -> Tuple[bool, str]:
    """产物校验：必须是可打开且至少 1 页的 PDF。"""
    if not os.path.exists(output_path) or os.path.getsize(output_path) < 100:
        return False, "转换产物为空"
    with open(output_path, "rb") as f:
        if f.read(5) != b"%PDF-":
            return False, "转换产物不是有效的 PDF"
    import fitz

    try:
        doc = fitz.open(output_path)
        pages = doc.page_count
        doc.close()
        if pages < 1:
            return False, "转换产物页数为 0"
    except Exception as e:
        return False, f"转换产物无法打开: {e}"
    return True, "ok"


async def convert_caj_to_pdf(
    input_path: str, output_path: str
) -> Tuple[bool, str, Optional[str]]:
    """CAJ → PDF 主入口。

    :return: (成功?, 消息, 内部类型标识)
    """
    ftype = detect_caj_type(input_path)
    logger.info(f"CAJ convert task: type={ftype} input={input_path}")

    if ftype == "pdf-embedded":
        # 伪后缀：内部即完整 PDF，直接落盘
        shutil.copyfile(input_path, output_path)
        ok, msg = _validate_output(output_path)
        return ok, msg, ftype

    if ftype in ("KDH", "unknown"):
        return (
            False,
            "暂不支持该文件格式（识别为 KDH 或未知类型）。"
            "建议使用 CAJViewer 打开后另存为 PDF。",
            ftype,
        )

    # CAJ / C8 / HN → caj2pdf 子进程转换
    loop = asyncio.get_running_loop()
    async with _SEM:
        ok, msg = await loop.run_in_executor(
            None, _run_convert, input_path, output_path
        )
    if not ok:
        return False, msg, ftype

    ok, msg = _validate_output(output_path)
    if not ok:
        return False, msg, ftype
    return True, "ok", ftype
