# -*- coding: utf-8 -*-
"""OFD→PDF 渲染回归测试。

样本来源（本机路径，目录/文件缺失时自动跳过，不影响 CI）：
- C:/home/小红书/*.ofd —— 用户收集的真实发票
- docs/xml-invoice/ofd/*.ofd —— 历史样本

断言分层：
- 所有样本：转换成功、页数>=1、文字非空（冒烟级）；
- 已知样本：按 EXPECTATIONS 里的文件名关键字触发针对性断言
  （矢量章存在、红色像素占比、文字颜色、标签/代码不重叠）。

新增样本：把 .ofd 放进上述任一目录即可自动纳入冒烟级回归；
若该样本有已确认的期望特征（如含监制章），在 EXPECTATIONS 登记
一条即可升级为特征级回归。
"""
import io
import os

import pytest

fitz = pytest.importorskip("fitz")
PIL_Image = pytest.importorskip("PIL.Image")

from app.services.ofd_converter import OFDConverter  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_DIRS = [
    r"C:/home/小红书",
    os.path.normpath(os.path.join(_HERE, "..", "..", "..", "docs", "xml-invoice", "ofd")),
]


def _collect_samples():
    files = []
    for d in SAMPLE_DIRS:
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if name.lower().endswith(".ofd"):
                files.append((name, os.path.join(d, name)))
    return sorted(files)


SAMPLES = _collect_samples()
if not SAMPLES:
    pytest.skip("无 OFD 样本可用", allow_module_level=True)


# 文件名关键字（小写包含匹配）→ 针对性期望
EXPECTATIONS = {
    # 铁路数电票：矢量监制章（CompositeObject）曾被整体丢失
    "25119110025000990799": dict(
        stamp_texts=("监制", "国家税务"),
        red_ellipses=2,
    ),
    # 机票式电子发票：红色标题/标签 + 信用代码与标签分离
    "机票电子发票-778": dict(
        red_text_rgb=(128, 0, 0),
        separation=True,
    ),
    # 保险数电票：模板线条颜色（行内 StrokeColor，ColorSpace 2）
    "永诚财产保险": dict(
        darkred_ratio=0.008,
    ),
    # 历史样本：仅冒烟（转换成功 + 文字完整）
}


def _expectation_for(name: str) -> dict:
    low = name.lower()
    for key, exp in EXPECTATIONS.items():
        if key.lower() in low:
            return exp
    return {}


def _render_page0(pdf_path: str):
    """返回 (fitz页对象, PIL 图像, 红色占比, 深红占比)。"""
    doc = fitz.open(pdf_path)
    page = doc[0]
    pix = page.get_pixmap(dpi=120)
    im = PIL_Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    px = list(im.getdata())
    n = max(1, len(px))
    red = sum(1 for r, g, b in px if r > 150 and g < 110 and b < 110)
    darkred = sum(1 for r, g, b in px if 90 < r <= 150 and g < 90 and b < 90)
    return doc, page, im, red / n, darkred / n


@pytest.mark.parametrize("name,path", SAMPLES, ids=[n for n, _ in SAMPLES])
def test_ofd_rendering(tmp_path, name, path):
    out = str(tmp_path / "out.pdf")
    converter = OFDConverter()
    ok, msg = converter.ofd_to_pdf(path, out)
    assert ok, f"转换失败: {msg}"

    doc, page, im, red_ratio, darkred_ratio = _render_page0(out)
    try:
        text = page.get_text()
        # 冒烟级：所有样本必须满足
        assert doc.page_count >= 1
        assert len(text) > 100, f"文字过少({len(text)}), 可能渲染缺失"

        exp = _expectation_for(name)

        # 特征级：矢量监制章存在
        if "stamp_texts" in exp:
            assert any(t in text for t in exp["stamp_texts"]), \
                f"监制章文字缺失: {exp['stamp_texts']}"
        if "red_ellipses" in exp:
            reds = [
                d for d in page.get_drawings()
                if d.get("color") and abs(d["color"][0] - 1) < 0.05
                and d["color"][1] < 0.1 and d["color"][2] < 0.1
            ]
            assert len(reds) >= exp["red_ellipses"], \
                f"红色椭圆不足: {len(reds)} < {exp['red_ellipses']}"

        # 特征级：文字颜色正确（红色标题/标签）
        if "red_text_rgb" in exp:
            found = False
            for b in page.get_text("dict")["blocks"]:
                for l in b.get("lines", []):
                    for s in l["spans"]:
                        col = s["color"]
                        rgb = ((col >> 16) & 255, (col >> 8) & 255, col & 255)
                        if rgb == exp["red_text_rgb"] and len(s["text"].strip()) >= 4:
                            found = True
            assert found, f"未找到颜色为 {exp['red_text_rgb']} 的红色文字"

        # 特征级：18 位信用代码与左侧标签不重叠
        # 注：OFD 原始几何中标签/代码 Boundary 本身有 ~1.4mm 设计性交叠
        # （字形实际不碰），修复前 bug 为 bbox 交叠 53.8pt，修复后 ~15pt，
        # 故阈值取 20pt 作为回归检测线。
        if exp.get("separation"):
            labels, codes = [], []
            for b in page.get_text("dict")["blocks"]:
                for l in b.get("lines", []):
                    for s in l["spans"]:
                        t = s["text"].strip()
                        if "纳税人识别号" in t:
                            labels.append(fitz.Rect(s["bbox"]))
                        elif len(t) == 18 and t[:2].isdigit():
                            codes.append(fitz.Rect(s["bbox"]))
            assert labels and codes, "未找到标签/代码文本"
            # 同一行有购/销两组：每个代码只与其「左侧最近的标签」配对
            label_centers = [(lb, (lb.x0 + lb.x1) / 2) for lb in labels]
            for c in codes:
                c_center = (c.x0 + c.x1) / 2
                left_labels = [lb for lb, cx in label_centers if cx < c_center]
                assert left_labels, "代码左侧未找到同组标签"
                lb = max(left_labels, key=lambda r: r.x1)
                overlap = lb.x1 - c.x0
                assert overlap < 20, \
                    f"代码与标签交叠 {overlap:.1f}pt（>20pt 视为字距回归）"

        # 特征级：深红像素占比（模板线条颜色）
        if "darkred_ratio" in exp:
            assert darkred_ratio >= exp["darkred_ratio"], \
                f"深红占比过低: {darkred_ratio:.4f} < {exp['darkred_ratio']}"
    finally:
        doc.close()
