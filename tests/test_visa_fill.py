# -*- coding: utf-8 -*-
"""XFA 签证表回填测试矩阵（docs/XFA_PHASE_1_HANDOFF.md §5，先于业务代码编写）。

被测服务契约（Phase 1-2 实现目标，apps/api/app/services/visa_fill.py）：
    fill_visa_form(template_id: str, values: dict[str, str]) -> bytes
    异常（router 层统一映射 422）：
        UnknownTemplateError      未知模板 ID
        InvalidSOMPathError       SOM 路径不在白名单（防注入任意 XML）
        ValueTooLongError         字段值超过 MAX_VALUE_LEN(500)
        InvalidFieldValueError    字段值含 XML 1.0 非法字符（Phase 1-2 验收补强）

红线（违反任何一条即测试失败）：
- 产出必须是增量更新：原始字节 100% 保留（append-only），禁全量重写
- datasets XML 改写前后：元素数一致(9755)、叶子路径集一致(255)、文本变化仅限目标字段
- DocMDP/UR3 双签名对象不得移动，ByteRange 覆盖区字节必须逐字节不变
- 模板必须使用受控目录下的原加密版（AES-128 R4），而非预解密工作版
"""

import io
import xml.etree.ElementTree as ET
from pathlib import Path

import pikepdf
import pytest

from app.services.visa_fill import (
    InvalidFieldValueError,
    InvalidSOMPathError,
    UnknownTemplateError,
    ValueTooLongError,
    fill_visa_form,
)

# ---------- 常量（Phase 0 实测基线，模板更换时须同步更新） ----------

TEMPLATE_ID = "imm5257e"
TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "app" / "templates" / "forms" / "imm5257e.pdf"
)

NS = {"xfa": "http://www.xfa.org/schema/xfa-data/1.0/"}
EXPECTED_XFA_PAIRS = 10
EXPECTED_XML_ELEMENTS = 9755
# 255 唯名叶子 + 同名兄弟索引化：Choice[0]/[1]（+1）、TextField1[0..4]（+4）、
# 根级脚本字段 TextField[0..8]（+8）——见 _index_siblings
EXPECTED_LEAF_PATHS = 268
MAX_VALUE_LEN = 500

# 与 Phase 0 的 11-incremental-fill.py FILL 字典一致（Adobe 五项验收全绿的样本值）
SAMPLE_VALUES = {
    "Page1/PersonalDetails/Name/FamilyName": "ZHANG",
    "Page1/PersonalDetails/Name/GivenName": "San",
    "Page1/PersonalDetails/DOBYear": "1990",
    "Page1/PersonalDetails/DOBMonth": "01",
    "Page1/PersonalDetails/DOBDay": "15",
    "Page1/PersonalDetails/PlaceBirthCity": "Beijing",
    "Page1/PersonalDetails/PlaceBirthCountry": "CHINA",
}


# ---------- 测试辅助（独立实现，不依赖服务内部函数） ----------


def _template_bytes() -> bytes:
    return TEMPLATE_PATH.read_bytes()


def _datasets_xml(pdf_bytes: bytes) -> str:
    """从 PDF 字节中提取 datasets 流的 XML 文本（pikepdf 解密态读取）。"""
    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        xfa = pdf.Root.AcroForm.XFA
        for i in range(0, len(xfa), 2):
            if str(xfa[i]) == "datasets":
                return xfa[i + 1].read_bytes().decode("utf-8")
        pytest.fail("XFA 数组中没有 datasets 流")


def _parse(xml: str) -> ET.Element:
    return ET.fromstring(xml)


def _form1(tree: ET.Element) -> ET.Element:
    data = tree.find("xfa:data", NS)
    assert data is not None, "datasets XML 缺少 xfa:data"
    kids = list(data)
    assert len(kids) >= 1, "xfa:data 下没有子节点"
    return kids[0]


def _leaf_texts(tree: ET.Element) -> dict[str, str | None]:
    """form1 子树下所有叶子路径 → 文本值（路径不含 form1 前缀，与服务 SOM 口径一致）。

    同名兄弟带 [n] 出现序号（与 _index_siblings 口径一致），否则同名
    节点互相折叠、与服务端白名单/写入口径漂移。
    """
    leaves: dict[str, str | None] = {}
    ns2 = "{http://www.xfa.org/schema/xfa-data/1.0/}"

    def local(tag: str) -> str:
        return tag.split("}")[-1]

    def index_siblings(children: list[ET.Element]) -> list[tuple[str, ET.Element]]:
        from collections import Counter

        counts = Counter(local(c.tag) for c in children)
        seen: dict[str, int] = {}
        out = []
        for c in children:
            name = local(c.tag)
            if counts[name] > 1:
                i = seen.get(name, 0)
                seen[name] = i + 1
                out.append((f"{name}[{i}]", c))
            else:
                out.append((name, c))
        return out

    def walk(node: ET.Element, prefix: str) -> None:
        for seg, child in index_siblings(list(node)):
            cur = f"{prefix}/{seg}" if prefix else seg
            if len(child) == 0:
                leaves[cur] = child.text
            else:
                walk(child, cur)

    walk(_form1(tree), "")
    return leaves


def _signature_info(pdf: pikepdf.Pdf) -> dict[str, tuple[int, list[int]]]:
    """提取 /Perms 下 DocMDP/UR3 签名对象的对象号与 ByteRange。

    注意：pikepdf 字典用纯字符串键取值会 KeyError（已实测），
    必须用 pikepdf.Name('/DocMDP') 形式。
    """
    info: dict[str, tuple[int, list[int]]] = {}
    perms = pdf.Root.Perms
    for key in ("DocMDP", "UR3"):
        sig = perms[pikepdf.Name("/" + key)]
        num, _gen = sig.objgen
        byte_range = [int(x) for x in sig.ByteRange]
        info[key] = (num, byte_range)
    return info


# ---------- Fixtures ----------


@pytest.fixture(scope="module")
def original_bytes() -> bytes:
    assert TEMPLATE_PATH.exists(), (
        f"受控模板缺失: {TEMPLATE_PATH}（应从原加密版复制，禁用预解密 work 版）"
    )
    return _template_bytes()


@pytest.fixture(scope="module")
def filled_bytes(original_bytes: bytes) -> bytes:
    return fill_visa_form(TEMPLATE_ID, SAMPLE_VALUES)


# ---------- 测试 0：模板资产健全性 ----------


def test_template_is_encrypted_original(original_bytes: bytes):
    """受控模板必须是 AES-128 R4 加密的原版（防止误用预解密 work 版）。"""
    with pikepdf.open(io.BytesIO(original_bytes)) as pdf:
        enc = pdf.trailer.Encrypt
        assert enc is not None, "模板未加密——疑似误用了预解密工作版"
        assert int(enc.R) == 4, f"加密版本 R={int(enc.R)}，应为 4（AESV2）"
        assert int(enc.V) == 4, f"加密字典 V={int(enc.V)}，应为 4"


# ---------- 测试 1：完整性（HANDOFF §5.1） ----------


def test_fill_integrity_xml_structure_unchanged(original_bytes: bytes, filled_bytes: bytes):
    """改写前后 datasets XML 元素数一致(9755)、叶子路径集一致(255)。"""
    orig_xml = _datasets_xml(original_bytes)
    new_xml = _datasets_xml(filled_bytes)

    orig_tree, new_tree = _parse(orig_xml), _parse(new_xml)

    orig_elems = list(orig_tree.iter())
    new_elems = list(new_tree.iter())
    assert len(new_elems) == len(orig_elems), "XML 元素数变化——结构被改写"
    assert len(new_elems) == EXPECTED_XML_ELEMENTS, (
        f"元素数 {len(new_elems)} != 基线 {EXPECTED_XML_ELEMENTS}"
    )

    orig_leaves = _leaf_texts(orig_tree)
    new_leaves = _leaf_texts(new_tree)
    assert set(new_leaves) == set(orig_leaves), "叶子路径集变化——结构被改写"
    assert len(new_leaves) == EXPECTED_LEAF_PATHS, (
        f"叶子路径数 {len(new_leaves)} != 基线 {EXPECTED_LEAF_PATHS}"
    )


def test_fill_integrity_only_target_fields_changed(
    original_bytes: bytes, filled_bytes: bytes
):
    """文本变化必须仅限目标字段，且新值精确匹配。"""
    orig_leaves = _leaf_texts(_parse(_datasets_xml(original_bytes)))
    new_leaves = _leaf_texts(_parse(_datasets_xml(filled_bytes)))

    changed = {
        path
        for path in orig_leaves
        if orig_leaves[path] != new_leaves.get(path)
    }
    assert changed == set(SAMPLE_VALUES), (
        f"文本变化集合与目标不符: 多改了 {changed - set(SAMPLE_VALUES)}, "
        f"漏改了 {set(SAMPLE_VALUES) - changed}"
    )
    for path, value in SAMPLE_VALUES.items():
        assert new_leaves[path] == value, f"字段 {path} 值错误: {new_leaves[path]!r}"


# ---------- 测试 2：增量更新 roundtrip（HANDOFF §5.2） ----------


def test_fill_roundtrip_append_only(original_bytes: bytes, filled_bytes: bytes):
    """产出文件必须是纯增量更新：原始字节 100% 保留 + 尾部追加。"""
    assert len(filled_bytes) > len(original_bytes), "产出未追加任何字节"
    assert filled_bytes[: len(original_bytes)] == original_bytes, (
        "原始字节被改动——这不是增量更新（红线 1）"
    )


def test_fill_roundtrip_pikepdf_reopen_values_readable(
    original_bytes: bytes, filled_bytes: bytes
):
    """产出文件 pikepdf 可重开（空密码解密），XFA 10 对，datasets 新值全部可读。"""
    with pikepdf.open(io.BytesIO(filled_bytes)) as pdf:
        xfa = pdf.Root.AcroForm.XFA
        assert len(xfa) // 2 == EXPECTED_XFA_PAIRS, (
            f"XFA 对数 {len(xfa) // 2} != {EXPECTED_XFA_PAIRS}"
        )
        new_leaves = _leaf_texts(_parse(_datasets_xml(filled_bytes)))
        for path, value in SAMPLE_VALUES.items():
            assert new_leaves[path] == value, f"重开后字段 {path} 不可读或值错误"


# ---------- 测试 3：双签名不被移动（HANDOFF §5.3） ----------


def test_fill_signature_objects_unmoved(original_bytes: bytes, filled_bytes: bytes):
    """DocMDP/UR3 签名对象号不变、ByteRange 不变、ByteRange 覆盖区字节逐字节一致。"""
    with pikepdf.open(io.BytesIO(original_bytes)) as orig_pdf, pikepdf.open(
        io.BytesIO(filled_bytes)
    ) as new_pdf:
        orig_info = _signature_info(orig_pdf)
        new_info = _signature_info(new_pdf)

        for key in ("DocMDP", "UR3"):
            orig_num, orig_br = orig_info[key]
            new_num, new_br = new_info[key]
            assert new_num == orig_num, (
                f"{key} 签名对象被移动: {orig_num} -> {new_num}"
            )
            assert new_br == orig_br, f"{key} ByteRange 变化: {orig_br} -> {new_br}"
            # ByteRange 覆盖区字节逐字节不变（签名验证的字面依据）
            for idx in range(0, len(orig_br), 2):
                start, length = orig_br[idx], orig_br[idx + 1]
                assert (
                    filled_bytes[start : start + length]
                    == original_bytes[start : start + length]
                ), f"{key} ByteRange 覆盖区 [{start}, {length}] 字节发生变化"


# ---------- 测试 4：非法输入拒绝（HANDOFF §5.4，服务层；422 映射在路由层测试） ----------


def test_reject_unknown_template():
    with pytest.raises(UnknownTemplateError):
        fill_visa_form("imm9999x", SAMPLE_VALUES)


def test_reject_invalid_som_path():
    # 路径遍历式注入
    with pytest.raises(InvalidSOMPathError):
        fill_visa_form(TEMPLATE_ID, {"../../Evil/Inject": "x"})
    # 格式合法但不在白名单
    with pytest.raises(InvalidSOMPathError):
        fill_visa_form(TEMPLATE_ID, {"Page1/Totally/Unknown/Path": "x"})
    # 歧义叶子名（多级后缀仍无法唯一解析，如 6 个 Choice）→ 拒绝
    with pytest.raises(InvalidSOMPathError):
        fill_visa_form(TEMPLATE_ID, {"Choice": "x"})


def test_resolve_form_tree_path_suffix():
    """前端 DOM 表单树路径（含 pdf.js 渲染包装层）必须经后缀解析命中数据树。

    Phase 1-3 实测：collectValues 拼出 Page1/form1/Page1/ButtonsHeader/AdultFlag，
    数据树是 Page1/AdultFlag——XFA bind 重定向导致两棵树不同构。
    """
    # 显式 bind 场景：仅叶子名后缀唯一
    out = fill_visa_form(
        TEMPLATE_ID,
        {"Page1/form1/Page1/ButtonsHeader/AdultFlag": "false"},
    )
    assert out[:5] == b"%PDF-"
    # 默认绑定场景：数据路径是表单路径后缀（真实节点名）
    out = fill_visa_form(
        TEMPLATE_ID,
        {
            "Page1/form1/Page1/PersonalDetails/Name/FamilyName": "ZHANG",
            "Page2/form1/Page2/MaritalStatus/SectionA/Passport/PassportNum/PassportNum": "E1234567",
        },
    )
    assert out[:5] == b"%PDF-"


def test_reject_value_too_long():
    with pytest.raises(ValueTooLongError):
        fill_visa_form(
            TEMPLATE_ID,
            {"Page1/PersonalDetails/Name/FamilyName": "A" * (MAX_VALUE_LEN + 1)},
        )


def test_reject_control_characters():
    """XML 1.0 非法字符必须前置拒绝为 422（而非序列化后 ParseError → 500）。

    Phase 1-2 验收实测：ET.tostring 对控制字符产出非法 XML，重开自检抛
    ParseError（SyntaxError 子类），路由层 except RuntimeError 接不住。
    """
    for bad in ("A\x00B", "A\x08B", "A\x1FB"):
        with pytest.raises(InvalidFieldValueError):
            fill_visa_form(
                TEMPLATE_ID, {"Page1/PersonalDetails/Name/FamilyName": bad}
            )
    # 合法空白字符（\t \n \r）不应被拒
    out = fill_visa_form(
        TEMPLATE_ID, {"Page1/PersonalDetails/Name/FamilyName": "A\tB"}
    )
    assert out[:5] == b"%PDF-"


def test_fill_same_and_empty_value_submission_ok():
    """同值/空值提交必须成功（Phase 1-2 验收 P0 修复的回归锁）。

    真实前端场景：collectValues() 整包回传全部字段值，包括
    ① 与模板预置值相同的字段（AdultFlag/FormVersion 等 7 个预置控件）；
    ② 用户未填的空字段（input.value = ""，ET 序列化后与 None 等价）。
    曾因 `changed == set(values)` 误判"漏改"→ RuntimeError → 500，
    每次真实提交必炸。
    """
    # ① 同值（AdultFlag 预置 'false'）
    out = fill_visa_form(TEMPLATE_ID, {"Page1/AdultFlag": "false"})
    assert out[:5] == b"%PDF-"

    # ② 空值（原为 None 的字段提交 ""）
    out = fill_visa_form(
        TEMPLATE_ID, {"Page1/PersonalDetails/Name/FamilyName": ""}
    )
    assert out[:5] == b"%PDF-"

    # ③ 混合：新值 + 同值 + 空值（模拟前端整包回传的最小切片）
    out = fill_visa_form(
        TEMPLATE_ID,
        {
            "Page1/PersonalDetails/Name/FamilyName": "ZHANG",
            "Page1/PersonalDetails/Name/GivenName": "",
            "Page1/AdultFlag": "false",
            "Page1/Header/CRCNum": "",
        },
    )
    assert out[:5] == b"%PDF-"
    assert len(out) > 1_400_000


# ---------- 测试 6：checkButton on 值翻译（Phase 1-3 用户 Adobe 冒烟发现） ----------


def test_checkbutton_on_value_translation():
    """勾选 "1" 必须翻译为模板 items 定义的 on 值，与 Adobe 真实会话一致。

    Adobe 会话黄金标本实测：SameAsCORIndicator 勾 Yes → 'Y'、PCRIndicator
    勾 No → 'N'。电话类型 CanadaUS 的 items 是三态 [1,0,2]（默认 0），
    勾选首击值即首项 '1'——Adobe 会话中的 '0' 是未勾选默认值被 Adobe
    整体重写持久化，不是勾选值。
    写死 "1" 时非 '1' on 值的按钮在 Adobe 显示未勾选（on 值不匹配）。
    前端 DOM 表单树路径（含 form1 噪音层）也必须经后缀唯一匹配命中。
    注意：AdultFlag/ApplicationValidatedFlag 是脚本托管的隐藏 textEdit
    字段（非 checkButton），不参与翻译，测试不覆盖。
    """
    out = fill_visa_form(
        TEMPLATE_ID,
        {
            # DOM 表单树路径（collectValues 实际回传形态）
            "Page1/form1/Page1/PersonalDetails/SameAsCORIndicator/Yes": "1",
            "Page1/form1/Page1/PersonalDetails/PCRIndicator/No": "1",
            "Page2/form1/Page2/ContactInformation/contact/PhoneNumbers/Phone/CanadaUS": "1",
        },
    )
    leaves = _leaf_texts(_parse(_datasets_xml(out)))
    assert leaves["Page1/PersonalDetails/SameAsCORIndicator"] == "Y"
    assert leaves["Page1/PersonalDetails/PCRIndicator"] == "N"
    assert leaves["Page2/ContactInformation/contact/PhoneNumbers/Phone/CanadaUS"] == "1"


def test_duplicate_sibling_occurrence_paths():
    """同名兄弟数据节点必须可分别寻址（Phase 1-3 用户 Adobe 冒烟发现的 P0）。

    datasets 里 BackgroundInfo 下有两个同名 <Choice>（1a 肺结核题 / 1b 障碍题
    两对 No/Yes 按钮的绑定节点）。按路径字符串作键时互相折叠：白名单只剩一条、
    写入字典后写覆盖先写 → Choice[0]（1a）从未被写入值，Adobe/pdf.js 一致地
    不显示 1a 勾选。修复后用 [n] 出现序号分别寻址。
    """
    out = fill_visa_form(
        TEMPLATE_ID,
        {
            "Page3/form1/Page3/BackgroundInfo/Choice[0]/Yes": "1",
            "Page3/form1/Page3/BackgroundInfo/Choice[1]/No": "1",
        },
    )
    assert out[:5] == b"%PDF-"
    # datasets 有两个同名 <Choice>，按路径的叶子字典会折叠，须按文档序分别断言
    tree = _parse(_datasets_xml(out))

    def local(tag: str) -> str:
        return tag.split("}")[-1]

    data = tree.find("xfa:data", NS)
    form1 = list(data)[0]
    page3 = [c for c in form1 if local(c.tag) == "Page3"][0]
    bi = [c for c in page3 if local(c.tag) == "BackgroundInfo"][0]
    choices = [c.text for c in bi if local(c.tag) == "Choice"]
    assert choices == ["Y", "N"], f"两个 Choice 节点应分别为 Y/N，实际 {choices}"


def test_checkbutton_explicit_on_value_passthrough():
    """直接提交 on 值（不经翻译）应原样写入；文本字段 "1" 不受翻译影响。"""
    out = fill_visa_form(
        TEMPLATE_ID,
        {
            "Page1/PersonalDetails/SameAsCORIndicator": "Y",
            "Page1/PersonalDetails/Name/FamilyName": "1",
        },
    )
    leaves = _leaf_texts(_parse(_datasets_xml(out)))
    assert leaves["Page1/PersonalDetails/SameAsCORIndicator"] == "Y"
    assert leaves["Page1/PersonalDetails/Name/FamilyName"] == "1"
