# -*- coding: utf-8 -*-
"""XFA 签证表增量回填服务（Phase 1-2，移植自 xfa-proto/phase0/11-incremental-fill.py）。

原理（Phase 0 已 Adobe 实测验收，勿改架构）：
- 唯一正确的修改方式 = 增量更新（append-only）：原文件字节 100% 保留，
  尾部追加新 datasets 流（AESV2 加密）+ XRef stream + startxref/%%EOF。
- 全量重写（pikepdf/pdf-lib save）会破坏 DocMDP/UR3 双签名 → Adobe 锁表单。
- XFA 数组对 datasets 的引用是同号间接引用（本模板 117 0 R）→ 同号重定义即可，
  AcroForm 无需改动。

红线（docs/XFA_PHASE_1_HANDOFF.md §〇）：
- stream 关键字后必须 \\r\\n（单独 \\r 非法，Adobe 弹"文件已损坏"）；
- XRef stream 用裸条目（不压缩不 Predictor——PNG Up 是算术减法非 XOR）；
- 算法 2 必须对 MD5 结果迭代 50 次（R≥3）；
- /ID 写回必须 <hex> 大写形式；
- 模板特定值（/Prev、/Root、/Info、/Encrypt、/Size、datasets 对象号）全部动态提取；
- 字段值零日志零落盘，纯内存处理。

pikepdf 实测 API 坑：
- stream.read_bytes() 已自动按 /Filter 解压，勿再 zlib.decompress；
- 字典取值必须 pikepdf.Name('/Xxx')，纯字符串键会 KeyError；
- 空闲对象判定：pdf.get_object((n, 0)) 返回 None（不抛异常）。
"""

import hashlib
import io
import secrets
import threading
import xml.etree.ElementTree as ET
import zlib
from collections import Counter
from pathlib import Path

import pikepdf
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pikepdf import Name

# ---------- 契约常量（测试矩阵固化） ----------

MAX_VALUE_LEN = 500
XFA_DATA_NS = "http://www.xfa.org/schema/xfa-data/1.0/"
NS = {"xfa": XFA_DATA_NS}
CRLF = b"\r\n"

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates" / "forms"


def _local(tag: str) -> str:
    return tag.split("}")[-1]


def _index_siblings(pairs: list[tuple[str, ET.Element]]) -> list[tuple[str, ET.Element]]:
    """给同名兄弟加 XFA occurrence 序号 [n]（如 Choice[0]/Choice[1]）。

    背景（Phase 1-3 用户 Adobe 冒烟实测）：datasets 里同名兄弟节点
    （BackgroundInfo 下两个 Choice，对应 1a/1b 两道题）在"路径字符串作键"
    的集合/字典里会互相折叠——白名单只剩一条、写入字典后写覆盖先写，
    导致第一个同名节点永远写不进值（pdf.js 与 Adobe 行为一致地不显示勾选）。
    同名兄弟全部带序号，唯名路径保持原样（既有口径兼容）。
    """
    counts = Counter(name for name, _ in pairs)
    seen: dict[str, int] = {}
    out: list[tuple[str, ET.Element]] = []
    for name, el in pairs:
        if counts[name] > 1:
            i = seen.get(name, 0)
            seen[name] = i + 1
            out.append((f"{name}[{i}]", el))
        else:
            out.append((name, el))
    return out


class VisaFillError(Exception):
    """签证表回填错误基类（路由层统一映射 422）。"""


class UnknownTemplateError(VisaFillError):
    """未知模板 ID。"""


class InvalidSOMPathError(VisaFillError):
    """SOM 路径不在白名单（防注入任意 XML）。"""


class ValueTooLongError(VisaFillError):
    """字段值超过长度限制。"""


class InvalidFieldValueError(VisaFillError):
    """字段值含 XML 1.0 非法字符（控制字符等）。"""


def _is_valid_xml_text(value: str) -> bool:
    """XML 1.0 合法字符判定（#x9|#xA|#xD|[#x20-#xD7FF]|[#xE000-#xFFFD]|[#x10000-#x10FFFF]）。
    实测教训：控制字符会让 ET.tostring 产出非法 XML，重开自检抛的是 ParseError
    （SyntaxError 子类，非 RuntimeError），路由层无法映射 → 公开端点 500。
    必须前置拒绝（Phase 1-2 验收发现）。"""
    for ch in value:
        cp = ord(ch)
        if not (
            cp in (0x9, 0xA, 0xD)
            or 0x20 <= cp <= 0xD7FF
            or 0xE000 <= cp <= 0xFFFD
            or 0x10000 <= cp <= 0x10FFFF
        ):
            return False
    return True


# ---------- AESV2 (R4) 加密三件套（11-incremental-fill.py 逐行移植，零改动） ----------

PAD = bytes([
    0x28, 0xBF, 0x4E, 0x5E, 0x4E, 0x75, 0x8A, 0x41, 0x64, 0x00, 0x4E, 0x56, 0xFF, 0xFA, 0x01, 0x08,
    0x2E, 0x2E, 0x00, 0xB6, 0xD0, 0x68, 0x3E, 0x80, 0x2F, 0x0C, 0xA9, 0xFE, 0x64, 0x53, 0x69, 0x7A])


def file_key_r4(pw: bytes, O: bytes, P: int, id0: bytes, n: int = 16) -> bytes:
    """PDF 算法 2 (R≥3)：MD5 拼接后，对前 n 字节再迭代 50 次 MD5，取前 n 字节。
    （50 次迭代是 R≥3 必需，漏了解密必败——Phase 0 v1 的元凶）"""
    h = hashlib.md5()
    h.update((pw + PAD)[:32])
    h.update(O[:32])
    h.update((P & 0xFFFFFFFF).to_bytes(4, "little"))
    h.update(id0)
    digest = h.digest()
    for _ in range(50):
        digest = hashlib.md5(digest[:n]).digest()
    return digest[:n]


def obj_key_aesv2(filekey: bytes, objnum: int, gen: int) -> bytes:
    """算法 3.2a (AESV2)：MD5(filekey + objnum_le3 + gen_le2 + 'sAlT')[:min(n+5,16)]"""
    h = hashlib.md5()
    h.update(filekey)
    h.update(objnum.to_bytes(3, "little"))
    h.update(gen.to_bytes(2, "little"))
    h.update(b"sAlT")
    return h.digest()[:min(len(filekey) + 5, 16)]


def aes_cbc_encrypt(key: bytes, data: bytes) -> bytes:
    """AES-128-CBC：随机 IV 前置 + PKCS7 填充（cryptography 库）。"""
    iv = secrets.token_bytes(16)
    p = padding.PKCS7(128).padder()
    padded = p.update(data) + p.finalize()
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return iv + enc.update(padded) + enc.finalize()


# ---------- 模板清单与缓存 ----------


def _load_manifest() -> dict:
    import json

    manifest_path = TEMPLATES_DIR / "forms.json"
    with open(manifest_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _leaf_paths_of(tree: ET.Element) -> set[str]:
    """form1 子树下所有叶子路径集合（路径不含 form1 前缀，与 SOM 白名单口径一致）。

    同名兄弟节点带 [n] 序号（见 _index_siblings）。
    """
    leaves: set[str] = set()

    def walk(node: ET.Element, prefix: str) -> None:
        pairs = _index_siblings([( _local(c.tag), c) for c in node])
        for seg, child in pairs:
            cur = f"{prefix}/{seg}" if prefix else seg
            if len(child) == 0:
                leaves.add(cur)
            else:
                walk(child, cur)

    data = tree.find("xfa:data", NS)
    if data is None or len(data) == 0:
        raise RuntimeError("模板 datasets XML 缺少 xfa:data/form1")
    form1 = list(data)[0]
    walk(form1, "")
    return leaves


class _TemplateInfo:
    __slots__ = ("raw", "leaf_paths", "cb_on_values")

    def __init__(
        self,
        raw: bytes,
        leaf_paths: set[str],
        cb_on_values: dict[str, str],
    ):
        self.raw = raw
        self.leaf_paths = leaf_paths
        self.cb_on_values = cb_on_values


_template_cache: dict[str, _TemplateInfo] = {}
_template_lock = threading.Lock()


def _get_template(template_id: str) -> _TemplateInfo:
    """加载并校验模板（sha256 防误换），缓存 datasets 叶子路径白名单。"""
    with _template_lock:
        info = _template_cache.get(template_id)
    if info is not None:
        return info

    manifest = _load_manifest()
    entry = manifest.get(template_id)
    if entry is None:
        raise UnknownTemplateError(f"未知表格模板: {template_id}")

    raw = (TEMPLATES_DIR / entry["file"]).read_bytes()
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != entry.get("pdf_sha256"):
        raise RuntimeError(
            f"模板文件校验失败（sha256 不符，疑似被误换）: {entry['file']}"
        )

    # 解析 datasets 白名单 + template checkButton on 值（同时验证加密态 R4，与测试断言双保险）
    with pikepdf.open(io.BytesIO(raw)) as pdf:
        enc = pdf.trailer[Name("/Encrypt")]
        if enc is None or int(enc.R) != 4:
            raise RuntimeError("模板不是预期的 AES-128 R4 加密原版")
        ds_stream = _find_datasets_stream(pdf)
        xml_text = ds_stream.read_bytes().decode("utf-8")
        tmpl_text = _find_template_stream(pdf).read_bytes().decode("utf-8")

    info = _TemplateInfo(
        raw=raw,
        leaf_paths=_leaf_paths_of(ET.fromstring(xml_text)),
        cb_on_values=_checkbutton_on_values(tmpl_text),
    )
    with _template_lock:
        _template_cache[template_id] = info
    return info


def _find_datasets_stream(pdf: pikepdf.Pdf) -> pikepdf.Stream:
    """遍历 XFA 数组找 datasets 流（动态获取其对象号）。"""
    xfa = pdf.Root.AcroForm.XFA
    for i in range(0, len(xfa), 2):
        if str(xfa[i]) == "datasets":
            return xfa[i + 1]
    raise RuntimeError("XFA 数组中没有 datasets 流")


def _find_template_stream(pdf: pikepdf.Pdf) -> pikepdf.Stream:
    """遍历 XFA 数组找 template 流。"""
    xfa = pdf.Root.AcroForm.XFA
    for i in range(0, len(xfa), 2):
        if str(xfa[i]) == "template":
            return xfa[i + 1]
    raise RuntimeError("XFA 数组中没有 template 流")


def _checkbutton_on_values(tmpl_text: str) -> dict[str, str]:
    """解析 template XML，返回 checkButton 字段表单树路径 → on 值。

    背景（Phase 1-3 用户 Adobe 冒烟实测）：XFA checkButton 勾选时写入
    datasets 的值**不是 1**，而是字段 <items> 里定义的 on 文本——
    Yes/No 互斥对是 exclGroup 下两个 field（各带 items 'Y'/'N'），
    单选钮如 ApplicationValidatedFlag 的 on 是 'Yes'，AdultFlag 是
    'adult'，电话类型按钮甚至是 '0'。写死 1 会导致 Adobe 显示未勾选。
    items 缺省时 XFA 默认 on=1。路径口径与 _leaf_paths_of 一致
    （从 form1 子级起、不含 form1 前缀）。
    """
    root = ET.fromstring(tmpl_text)

    def local(tag: str) -> str:
        return tag.split("}")[-1]

    def has_checkbutton(field: ET.Element) -> bool:
        return any(local(el.tag) == "checkButton" for el in field.iter())

    def first_items_text(field: ET.Element) -> str | None:
        for child in field:
            if local(child.tag) == "items":
                for t in child:
                    if local(t.tag) == "text" and t.text and t.text.strip():
                        return t.text.strip()
        return None

    out: dict[str, str] = {}

    def walk(node: ET.Element, prefix: str) -> None:
        named = [(c.attrib.get("name"), c) for c in node if c.attrib.get("name")]
        seg_by_id = {id(el): seg for seg, el in _index_siblings(named)}
        for child in node:
            name = child.attrib.get("name")
            if name is None:
                # 匿名容器（如 area）不进入路径，透传前缀
                walk(child, prefix)
                continue
            seg = seg_by_id.get(id(child), name)
            cur = f"{prefix}/{seg}" if prefix else seg
            tag = local(child.tag)
            if tag == "field" and has_checkbutton(child):
                out[cur] = first_items_text(child) or "1"
            walk(child, cur)

    form1 = None
    for child in root:
        if local(child.tag) == "subform" and child.attrib.get("name") == "form1":
            form1 = child
            break
    if form1 is None:
        raise RuntimeError("模板 template XML 缺少根 subform form1")
    walk(form1, "")
    return out


def _prev_xref_offset(raw: bytes) -> int:
    """从模板原始字节提取最后一个 startxref 值（增量段的 /Prev）。"""
    idx = raw.rfind(b"startxref")
    if idx < 0:
        raise RuntimeError("模板缺少 startxref")
    tail = raw[idx + len(b"startxref"): idx + 64]
    token = tail.split()[0] if tail.split() else b""
    try:
        return int(token)
    except ValueError as e:
        raise RuntimeError(f"startxref 值解析失败: {token!r}") from e


def _free_object_number(pdf: pikepdf.Pdf) -> int:
    """从 /Size 起找第一个空闲对象号（get_object 返回 None 即空闲）。
    本模板：Size=133 → 空闲槽 133，与 Phase 0 黄金标本布局同构。"""
    n = int(pdf.trailer[Name("/Size")])
    while pdf.get_object((n, 0)) is not None:
        n += 1
    return n


def _resolve_som(key: str, leaf_paths: set[str]) -> str:
    """把字段键解析为 datasets 数据树全路径（白名单成员）。

    背景（Phase 1-3 实测）：前端 collectValues 从 pdf.js XFA DOM 拼出的是
    **表单树**路径（如 Page1/form1/Page1/ButtonsHeader/AdultFlag），与
    datasets **数据树**（Page1/AdultFlag）因 XFA bind 重定向与渲染包装层
    （xfaPage/form1 等）不同构——数据路径不一定是 key 的连续后缀。

    算法：按 key 的各级后缀（从长到短）收集「以其结尾的白名单路径」，
    返回首个**恰好唯一**的后缀层；扫到最短仍不唯一（如 6 个 Choice）
    或零命中 → 拒绝，防错填。最终命中必为白名单成员，注入面不变。
    """
    if key in leaf_paths:
        return key
    parts = key.split("/")
    for i in range(1, len(parts)):
        suffix = "/" + "/".join(parts[i:])
        matches = [p for p in leaf_paths if p.endswith(suffix)]
        if len(matches) == 1:
            return matches[0]
    # checkButton 选项尾巴（Phase 1-3 实测）：pdf.js 渲染 Yes/No 圆钮时 DOM 叶子
    # 是选项名（.../Indicator/Yes），数据树叶子是 indicator 本身（勾选值由
    # _translate_checkbutton 按 items on 值翻译）——剥掉尾段后重试同一算法
    if parts and parts[-1] in ("Yes", "No"):
        key2_parts = parts[:-1]
        key2 = "/".join(key2_parts)
        if key2 in leaf_paths:
            return key2
        for i in range(1, len(key2_parts)):
            suffix = "/" + "/".join(key2_parts[i:])
            matches = [p for p in leaf_paths if p.endswith(suffix)]
            if len(matches) == 1:
                return matches[0]
    raise InvalidSOMPathError("包含无效的表单字段路径")


def _translate_checkbutton(key: str, value: str, cb_on_values: dict[str, str]) -> str:
    """把前端勾选约定的 "1" 翻译为该 checkButton 字段的 on 值。

    前端 collectValues 对勾选框统一回传 "1"；而 XFA 数据值须为模板
    <items> 定义的 on 文本（'Y'/'N'/'Yes'/'adult'/'0'…）。按 key 的
    各级后缀（从长到短）在 on 值表中找**恰好唯一**的 checkButton
    字段路径；未命中则原样返回（数据叶子合法地填 "1" 的文本字段不受影响）。
    """
    if value != "1" or not cb_on_values:
        return value
    if key in cb_on_values:
        return cb_on_values[key]
    parts = key.split("/")
    for i in range(1, len(parts)):
        suffix = "/" + "/".join(parts[i:])
        matches = [p for p in cb_on_values if p.endswith(suffix)]
        if len(matches) == 1:
            return cb_on_values[matches[0]]
    return value


# ---------- XML 改写（ElementTree，禁正则） ----------


def _apply_values(xml_text: str, resolved: dict[str, str]) -> str:
    """按已解析的数据树全路径改写 datasets XML 叶子文本。

    Args:
        resolved: 数据树全路径（白名单成员）→ 提交值。键已经
            _resolve_som 解析，不再接受原始表单树路径。
    """
    ET.register_namespace("xfa", XFA_DATA_NS)
    tree = ET.fromstring(xml_text)

    leaves: dict[str, ET.Element] = {}

    def walk(node: ET.Element, prefix: str) -> None:
        for seg, child in _index_siblings([(_local(c.tag), c) for c in node]):
            cur = f"{prefix}/{seg}" if prefix else seg
            if len(child) == 0:
                leaves[cur] = child
            else:
                walk(child, cur)

    data = tree.find("xfa:data", NS)
    if data is None or len(data) == 0:
        raise RuntimeError("模板 datasets XML 缺少 xfa:data/form1")
    form1 = list(data)[0]
    walk(form1, "")

    for som, val in resolved.items():
        node = leaves.get(som)
        if node is None:
            # 白名单解析在前，这里命中失败说明解析与遍历口径漂移——防御性断言
            raise RuntimeError(f"内部错误：白名单路径未命中遍历树: {som}")
        node.text = val

    return ET.tostring(tree, encoding="unicode", xml_declaration=False)


# ---------- 完整性自检（HANDOFF §三.4，失败绝不发坏文件） ----------


def _verify_integrity(
    orig_xml: str, new_xml: str, values: dict[str, str]
) -> None:
    orig_tree = ET.fromstring(orig_xml)
    new_tree = ET.fromstring(new_xml)

    if len(list(orig_tree.iter())) != len(list(new_tree.iter())):
        raise RuntimeError("完整性自检失败：XML 元素数变化")

    def leaf_texts(tree: ET.Element) -> dict[str, str | None]:
        result: dict[str, str | None] = {}

        def walk(node: ET.Element, prefix: str) -> None:
            for seg, child in _index_siblings([(_local(c.tag), c) for c in node]):
                cur = f"{prefix}/{seg}" if prefix else seg
                if len(child) == 0:
                    result[cur] = child.text
                else:
                    walk(child, cur)

        data = tree.find("xfa:data", NS)
        form1 = list(data)[0]
        walk(form1, "")
        return result

    orig_leaves = leaf_texts(orig_tree)
    new_leaves = leaf_texts(new_tree)
    if set(orig_leaves) != set(new_leaves):
        raise RuntimeError("完整性自检失败：叶子路径集变化")

    # 语义（Phase 1-2 验收修正）：
    # ① 变化集合必须是目标字段的子集（不许多改）；
    # ② 每个目标字段的最终值必须精确等于提交值（必须写入）。
    # 注意不能用 `changed == set(values)`——用户提交与原值相同的值
    # （如模板预置的 AdultFlag/FormVersion，前端 collectValues 整包回传）
    # 是合法的"无变化"提交，曾被误判为漏改 → 500。
    changed = {p for p in orig_leaves if orig_leaves[p] != new_leaves.get(p)}
    if not changed <= set(values):
        raise RuntimeError(
            f"完整性自检失败：出现非目标字段文本变化 "
            f"(多改 {sorted(changed - set(values))[:5]}...)"
        )
    for som, val in values.items():
        actual = new_leaves.get(som)
        # None ≡ ""：ET 序列化把空串归一为自闭合标签（解析回 None），
        # 前端 input.value="" 的空字段提交是合法等价形态
        if (actual or "") != (val or ""):
            # 零值日志红线：异常消息只含字段路径，不含字段值
            raise RuntimeError(
                f"完整性自检失败：目标字段未正确写入 ({som})"
            )


# ---------- 主入口 ----------


def fill_visa_form(template_id: str, values: dict[str, str]) -> bytes:
    """回填签证表并返回增量更新后的 PDF 字节。

    Raises:
        UnknownTemplateError: 未知模板 ID
        InvalidSOMPathError:  SOM 路径不在白名单
        ValueTooLongError:    字段值超过 MAX_VALUE_LEN
        RuntimeError:         模板损坏 / 完整性自检失败（路由层映射 500）
    """
    if not values:
        raise InvalidSOMPathError("字段值列表为空")
    if len(values) > 1000:
        raise InvalidSOMPathError("字段数量超限")

    tpl = _get_template(template_id)  # 触发加载 + 白名单缓存

    # 1. 输入校验（先于任何改写）：类型/长度 + 非法字符 + SOM 解析到数据树全路径
    resolved: dict[str, str] = {}
    for som, val in values.items():
        if not isinstance(val, str) or len(val) > MAX_VALUE_LEN:
            raise ValueTooLongError(
                f"字段值超过长度限制（≤{MAX_VALUE_LEN} 字符）"
            )
        if not _is_valid_xml_text(val):
            raise InvalidFieldValueError("字段值含不支持的字符（控制/二进制字符）")
        if not isinstance(som, str):
            raise InvalidSOMPathError("包含无效的表单字段路径")
        val = _translate_checkbutton(som, val, tpl.cb_on_values)
        resolved[_resolve_som(som, tpl.leaf_paths)] = val
    orig = tpl.raw
    orig_len = len(orig)

    # 2. pikepdf 打开（空密码解密态），动态提取全部模板特定值
    with pikepdf.open(io.BytesIO(orig)) as pdf:
        enc = pdf.trailer[Name("/Encrypt")]
        if int(enc.R) != 4:
            raise RuntimeError(f"非 R4 加密模板 (R={int(enc.R)})")

        ds_stream = _find_datasets_stream(pdf)
        dsnum, dsgen = ds_stream.objgen
        orig_xml = ds_stream.read_bytes().decode("utf-8")

        O = bytes(enc.O)
        P = int(enc.P)
        id0 = bytes(pdf.trailer.ID[0])
        root_num, root_gen = pdf.Root.objgen
        info_num, info_gen = pdf.trailer[Name("/Info")].objgen
        enc_num, enc_gen = enc.objgen
        xref_num = _free_object_number(pdf)

    prev_offset = _prev_xref_offset(orig)

    # 3. 改写 XML + 压缩 + AESV2 加密新 datasets 流
    new_xml = _apply_values(orig_xml, resolved)
    _verify_integrity(orig_xml, new_xml, resolved)  # 改写侧自检

    compressed = zlib.compress(new_xml.encode("utf-8"))
    fkey = file_key_r4(b"", O, P, id0)
    okey_ds = obj_key_aesv2(fkey, dsnum, dsgen)
    cipher_ds = aes_cbc_encrypt(okey_ds, compressed)

    # 4. 构造增量段（行尾/裸条目/ID 大写 hex 纪律见模块 docstring）
    obj_ds = (
        f"{dsnum} {dsgen} obj".encode() + CRLF +
        b"<< /Filter /FlateDecode /Length " + str(len(cipher_ds)).encode() + b" >>" + CRLF +
        b"stream" + CRLF +
        cipher_ds + CRLF +
        b"endstream" + CRLF +
        b"endobj" + CRLF
    )

    off_ds = orig_len
    off_xref = orig_len + len(obj_ds)
    # W[1 3 0] 偏移量仅 3 字节（上限 0xFFFFFF）；本模板 ~1.45MB 远未触限，
    # 此护栏防未来大模板静默 OverflowError（Phase 1-2 验收加固）
    if off_xref > 0xFFFFFF:
        raise RuntimeError("增量段偏移超出 W[1 3 0] 表示上限")
    raw_entries = (
        bytes([1]) + off_ds.to_bytes(3, "big")
        + bytes([1]) + off_xref.to_bytes(3, "big")
    )
    new_id1 = secrets.token_bytes(16)

    xref_dict = (
        f"<< /Encrypt {enc_num} {enc_gen} R".encode()
        + b" /ID [<" + id0.hex().upper().encode() + b"><" + new_id1.hex().upper().encode() + b">]"
        + f" /Index [{dsnum} 1 {xref_num} 1]".encode()
        + f" /Info {info_num} {info_gen} R".encode()
        + b" /Length " + str(len(raw_entries)).encode()
        + f" /Prev {prev_offset}".encode()
        + f" /Root {root_num} {root_gen} R".encode()
        + f" /Size {xref_num + 1}".encode()
        + b" /Type /XRef /W [1 3 0] >>"
    )
    obj_xref = (
        f"{xref_num} 0 obj".encode() + CRLF + xref_dict + CRLF +
        b"stream" + CRLF + raw_entries + CRLF +
        b"endstream" + CRLF + b"endobj" + CRLF
    )
    startxref_block = (
        b"startxref" + CRLF + str(off_xref).encode() + CRLF + b"%%EOF" + CRLF
    )

    out = orig + obj_ds + obj_xref + startxref_block

    # 5. 结构自检：原字节保留 + 可重开 + datasets 新值可读
    if out[:orig_len] != orig:
        raise RuntimeError("完整性自检失败：原始字节被改动")
    with pikepdf.open(io.BytesIO(out)) as check:
        check_xml = _find_datasets_stream(check).read_bytes().decode("utf-8")
    _verify_integrity(orig_xml, check_xml, resolved)

    return out
