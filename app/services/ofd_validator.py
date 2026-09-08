"""OFD 文件安全预扫描与并发转换。

设计依据：docs/OFD_VIEWER_DESIGN.md v1.1 §4.3（开发期对抗审查加固）
- 快速预过滤四规则（条目数 / 解压总量 / 压缩比 / zip slip 双保险），只读
  中央目录，开销极小；
- 规则 5（防伪造中央目录）：逐条目流式核对实际解压大小与声明一致——
  中央目录的 file_size 可被伪造（声明很小、实际解压巨大）以绕过总量与
  压缩比两条规则，此处按「声明+1 字节」封顶读取，多读出一个字节即拒绝；
- 加密检测：中央目录 flag_bits & 0x1（PoC 实测 15/15 命中）；
- 并发限制：Semaphore(2) + 线程池执行 CPU 密集转换，修复原 /ofd-to-pdf
  在 async def 内同步调用导致的整个事件循环阻塞。

⚠ 单 worker 前提（写死）：asyncio.Semaphore 是进程级的，uvicorn 多 worker
下实际并发 = 2 × workers 数。当前部署为单 worker；扩容 worker 前必须重估
并发上限与内存预算（200MB 解压上限与并发 2 为联动参数）。
"""

import asyncio
import multiprocessing
import os
import shutil
import threading
import zipfile

import fitz

from app.core.logger import get_logger
from app.services.ofd_converter import OFDConverter

logger = get_logger(__name__)

# ---- 预扫描阈值（PoC 实测定稿）----
# 条目数：300 页文本公文 303 / 图片型 604，5000 留足余量（500 会误杀大文档）
MAX_ENTRIES = 5000
# 解压总量（zip bomb 主防线；与并发 2 联动，见设计文档 §4.3 内存预算核算）
MAX_TOTAL_UNCOMPRESSED = 200 * 1024 * 1024
# 单文件压缩比（zip bomb 变体）
MAX_COMPRESSION_RATIO = 100
# 小于该大小的文件不做压缩比判断（小文件天然高压缩比，避免误伤）
MIN_RATIO_FILE_SIZE = 4096

# 转换超时（PoC 实测定稿：300 页服务器 15.8s × ~4 倍余量）
CONVERSION_TIMEOUT_SECONDS = 60

# 单 worker 前提下的进程级并发上限
_CONCURRENCY = 2
_semaphore = asyncio.Semaphore(_CONCURRENCY)


class OfdFileError(Exception):
    """预扫描拦截：不是有效的 OFD 文件（ZIP 结构校验失败）。"""


class OfdEncryptedError(OfdFileError):
    """OFD 文件已加密。"""


def validate_ofd_zip(path: str) -> None:
    """对已落盘的 OFD 文件做安全预扫描。

    抛出 OfdEncryptedError / OfdFileError；通过则静默返回。
    在调用 easyofd 之前执行（easyofd 的 extract 无任何大小/数量限制）。

    说明：规则 1-4 只读中央目录；规则 5 会流式解压核对（上限=声明+1 字节
    /条目），Σ声明已被规则 2 限制在 200MB 内，最坏情况多花一次解压的
    CPU（秒级），换来对伪造中央目录攻击的免疫。
    """
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
            names = zf.namelist()

            if not infos:
                raise OfdFileError("ZIP 内无任何条目")

            # 加密检测：任一成员置位加密标志即拒绝（须在 zf.open 之前，
            # 否则规则 5 对加密成员 open 读流会抛 RuntimeError）
            if any(info.flag_bits & 0x1 for info in infos):
                raise OfdEncryptedError("OFD 文件已加密")

            # 规则 1：条目数
            if len(infos) > MAX_ENTRIES:
                raise OfdFileError(f"条目数超限（{len(infos)} > {MAX_ENTRIES}）")

            # 规则 2：解压总量（zip bomb）
            total = sum(info.file_size for info in infos)
            if total > MAX_TOTAL_UNCOMPRESSED:
                raise OfdFileError(
                    f"解压后总量超限（{total} > {MAX_TOTAL_UNCOMPRESSED}）"
                )

            # 规则 3：单文件压缩比（zip bomb 变体）
            for info in infos:
                if info.file_size >= MIN_RATIO_FILE_SIZE and info.compress_size > 0:
                    if info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                        raise OfdFileError(f"检测到异常压缩比（{info.filename}）")

            # 规则 4：zip slip 双保险（ZipFile.extract 本身已 sanitize，此处防御纵深）
            for name in names:
                normalized = name.replace("\\", "/")
                if normalized.startswith("/") or (
                    len(normalized) >= 2 and normalized[1] == ":"
                ):
                    raise OfdFileError(f"包含绝对路径条目（{name}）")
                if any(part == ".." for part in normalized.split("/")):
                    raise OfdFileError(f"包含路径穿越条目（{name}）")

            # 规则 5：实际解压大小与声明一致（防伪造中央目录的 zip bomb 绕过）。
            # 分块流式解压、只数长度不攒内容：诚实文件恰好数到 file_size 字节；
            # 被伪造声明的条目会多读出一个字节，立即暴露。单文件声明被规则 2/3
            # 限制，分块后本步骤内存增量恒为 1MB/次（2C2G 友好）。截断流留给
            # 转换阶段的 CRC 校验兜底。
            for info in infos:
                actual_len = 0
                with zf.open(info) as fh:
                    while True:
                        chunk = fh.read(1024 * 1024)
                        if not chunk:
                            break
                        actual_len += len(chunk)
                        if actual_len > info.file_size:
                            raise OfdFileError(
                                f"条目声明大小与实际不符（{info.filename}）"
                            )
    except zipfile.BadZipFile:
        raise OfdFileError("不是有效的 ZIP 结构")


def _visible_bbox(page: "fitz.Page", zoom: float = 0.2, tol: int = 245) -> "fitz.Rect":
    """渲染像素法求页面「可见内容」包围盒（非白色像素的极值范围）。

    为什么不用矢量元素并集（get_drawings/get_text）：OFD 模板补丁会画整页
    白色背景填充矩形、水印注释层也会铺满页面——这些元素把包围盒撑满整页，
    让「内容只占左上角 36%」的样本误判为已铺满（2026-09-08 实测翻车）。
    渲染成位图后只看非白像素，不可见填充天然免疫。

    zoom=0.2 时 1653pt 页 ≈ 330px，单页纯 Python 扫描 ~50ms；整行全白时
    按字节最小值快速跳过。tol=245 容忍抗锯齿灰边。
    """
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    w, h, n = pix.width, pix.height, pix.n
    if w == 0 or h == 0:
        return fitz.Rect()
    samples = pix.samples
    stride = pix.stride
    minx, miny, maxx, maxy = w, h, -1, -1
    for y in range(h):
        row = samples[y * stride : y * stride + w * n]
        if min(row) == 255:
            continue  # 整行纯白（含 alpha 通道恒 255 的情况）
        for x in range(w):
            off = x * n
            if row[off] < tol or row[off + 1] < tol or row[off + 2] < tol:
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
                if y < miny:
                    miny = y
                maxy = y
    if maxx < 0:
        return fitz.Rect()  # 整页纯白
    return fitz.Rect(minx / zoom, miny / zoom, (maxx + 1) / zoom, (maxy + 1) / zoom)


def normalize_pdf(
    src: str, dst: str, threshold_pt: float = 800.0, crop: bool = True
) -> str:
    """把 easyofd 转出的超大 PDF 等比归一到正常物理尺寸（矢量保真）。

    两类失真都要修：
    1. MediaBox 放大 25/9（内容随页面等比放大，整页缩回即可）；
    2. 「页面 200/25.4 mm→pt、内容 72/25.4 空间」的坐标系不一致（铁路客票
       等样本，票面只画在页面左上角 ~36% 区域）——整页缩放会保留内容在
       页内的相对位置，必须按可见内容包围盒裁剪重建（clip + show_pdf_page）。
    可见范围用渲染像素法测（_visible_bbox），整页白色模板底/水印不会干扰。
    策略：可见内容显著小于页面（<90%）时裁剪重建，否则整页等比缩放；
    裁剪时四周留 6pt 白边避免票面贴边。任何异常 fallback 复制原文件。

    crop 参数（P2-4 语义区分）：
    - crop=True（默认）：启用缩角裁剪。用于 /ofd/view 与发票合并——屏幕查看
      与发票拼版都希望内容铺满页面；
    - crop=False：跳过逐页 bbox 检测，只做整页等比缩放。用于 /ofd-to-pdf
      下载链路——下载文件的物理尺寸应忠实于原文档（页边距保留，逐页尺寸
      一致），裁剪会改变打印表现。

    原实现位于 invoice_merge_shared（发票合并专用），2026-09-08 对抗审查后
    上移至此作为 OFD→PDF 转换管线的公共步骤，发票合并改为从此导入。
    """
    doc = fitz.open(src)
    try:
        if doc[0].rect.width <= threshold_pt:
            # 尺寸已正常，直接复制
            doc.close()
            shutil.copyfile(src, dst)
            return dst
        out = fitz.open()
        for p in doc:
            do_crop = False
            bbox = fitz.Rect()
            if crop:
                bbox = _visible_bbox(p)
                # 可见内容显著小于页面 → 裁剪到内容重建（修左上角缩角）
                do_crop = (
                    not bbox.is_empty
                    and bbox.width > 20
                    and (
                        bbox.width < p.rect.width * 0.9
                        or bbox.height < p.rect.height * 0.9
                    )
                )
            if do_crop:
                pad = 6.0
                clip = fitz.Rect(
                    max(bbox.x0 - pad, 0),
                    max(bbox.y0 - pad, 0),
                    min(bbox.x1 + pad, p.rect.width),
                    min(bbox.y1 + pad, p.rect.height),
                )
                k = 595.0 / clip.width
                np_ = out.new_page(width=clip.width * k, height=clip.height * k)
                np_.show_pdf_page(np_.rect, doc, p.number, clip=clip)
            else:
                k = 595.0 / p.rect.width
                np_ = out.new_page(width=p.rect.width * k, height=p.rect.height * k)
                np_.show_pdf_page(np_.rect, doc, p.number)
        doc.close()
        out.save(dst)
        out.close()
        return dst
    except Exception as e:
        logger.warning(f"normalize_pdf failed, fallback copy: {e}")
        try:
            doc.close()
        except Exception:
            pass
        shutil.copyfile(src, dst)
        return dst


def _convert_job(input_path: str, output_path: str, crop: bool) -> tuple[bool, str]:
    """进程池 worker：执行 OFD→PDF 转换 + 归一化（P2-5 进程隔离）。

    在独立进程执行的价值：
    1. CPU 密集转换不再与 API 事件循环争 GIL（2C2G 单机收益明显）；
    2. easyofd/pymupdf 段错误等硬崩溃只死 worker，不拖垮 API 进程；
    3. 超时可真取消——父进程 terminate 掉池内 worker，不再有「僵尸转换
       继续跑完烧几十秒 CPU」的问题（线程方案无法取消）。

    worker 由 maxtasksperchild 定期回收，防长跑内存膨胀。
    """
    converter = OFDConverter()
    ok, msg = converter.ofd_to_pdf(input_path, output_path)
    if ok:
        # 归一化经临时文件中转（normalize 的 fallback copy 不支持同路径）
        tmp_out = output_path + ".norm.pdf"
        try:
            normalize_pdf(output_path, tmp_out, crop=crop)
            os.replace(tmp_out, output_path)
        except Exception as e:
            logger.warning(f"normalize step skipped: {e}")
            try:
                if os.path.exists(tmp_out):
                    os.remove(tmp_out)
            except OSError:
                pass
    return ok, msg


# 惰性创建的转换进程池（首次 OFD 转换才付 fork/spawn 成本；worker 数与
# _CONCURRENCY 信号量一致，池本身也天然限并发）
_mp_pool = None
_mp_lock = threading.Lock()


def _get_mp_pool() -> "multiprocessing.Pool":
    global _mp_pool
    with _mp_lock:
        if _mp_pool is None:
            _mp_pool = multiprocessing.Pool(
                processes=_CONCURRENCY, maxtasksperchild=100
            )
        return _mp_pool


def _destroy_mp_pool() -> None:
    """terminate 掉整个转换池（超时/池异常时调用），下次调用惰性重建。"""
    global _mp_pool
    with _mp_lock:
        if _mp_pool is not None:
            try:
                _mp_pool.terminate()
                _mp_pool.join()
            except Exception:
                pass
            _mp_pool = None


async def convert_ofd_to_pdf(
    input_path: str, output_path: str, crop: bool = True
) -> tuple[bool, str]:
    """执行 OFD→PDF 转换：进程池隔离 + 信号量限并发 + 超时真取消（P2-5）。

    crop 透传给 normalize_pdf（P2-4）：/ofd/view 与发票合并用默认 True
    （缩角裁剪）；/ofd-to-pdf 传 False 保持文档物理尺寸语义。

    超时语义：terminate 池内 worker 真取消僵尸转换；代价是同池另一进行中
    转换也会被终止——超时本身罕见（60s 上限 + 压测 300 页仅 15s），且被
    终止方会收到明确的失败返回，可重试。进程池不可用时（极端环境）自动
    降级为线程池执行（保留旧路径的全部行为，仅失去真取消能力）。
    """
    loop = asyncio.get_running_loop()
    async with _semaphore:
        pool = None
        try:
            pool = await loop.run_in_executor(None, _get_mp_pool)
        except Exception as e:
            logger.warning(f"conversion process pool unavailable, using thread: {e}")
            pool = None
        try:
            if pool is not None:
                return await asyncio.wait_for(
                    loop.run_in_executor(
                        None, pool.apply, _convert_job, (input_path, output_path, crop)
                    ),
                    timeout=CONVERSION_TIMEOUT_SECONDS,
                )
            # 降级路径：线程池执行（无进程隔离，行为与旧实现一致）
            return await asyncio.wait_for(
                loop.run_in_executor(
                    None, _convert_job, input_path, output_path, crop
                ),
                timeout=CONVERSION_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.error(
                f"OFD conversion timed out after {CONVERSION_TIMEOUT_SECONDS}s: {input_path}"
            )
            if pool is not None:
                # 真取消：杀掉池内 worker，杜绝僵尸转换继续占 CPU
                await loop.run_in_executor(None, _destroy_mp_pool)
            return (
                False,
                f"转换超时（超过 {CONVERSION_TIMEOUT_SECONDS} 秒），请稍后重试",
            )


def count_pdf_pages(path: str) -> int:
    """统计转换输出 PDF 的页数（供 /ofd/view 返回协议使用）。"""
    import fitz

    doc = fitz.open(path)
    try:
        return doc.page_count
    finally:
        doc.close()
