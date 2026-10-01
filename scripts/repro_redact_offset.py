"""框选涂黑错位复现 v2：像素级检查黑块真实落点。

实验 A：普通页面（CropBox 原点 0,0）——两个框：第 2 行 / 第 5-10 行。
实验 B：CropBox 偏移 (50,30) 的页面——同样两个框。
检查：fitz 渲染输出页，逐行扫描黑色像素，输出黑块 y 范围并与期望行位置对比。
"""
import sys

sys.path.insert(0, ".")
import fitz

from app.services.redactor import redact

PAGE_W, PAGE_H = 595.27, 841.89


def make_pdf(crop_offset=(0, 0), n_lines=15):
    doc = fitz.open()
    page = doc.new_page(width=PAGE_W, height=PAGE_H)
    for i in range(n_lines):
        text = f"LINE-{i + 1:02d} " + "x" * 40
        y = 750 - i * 45  # 基线（y-up）：第1行 750 ... 第15行 120
        page.insert_text(fitz.Point(72, y), text, fontsize=12)
    if crop_offset != (0, 0):
        page.set_cropbox(fitz.Rect(crop_offset[0], crop_offset[1], PAGE_W, PAGE_H))
    data = doc.tobytes()
    doc.close()
    return data


def line_top_y(baseline):  # 文字块顶部（fontsize 12 → 高约 14）
    return baseline + 14


def black_bands(pdf_bytes, zoom=1.0):
    """渲染第 1 页，返回黑色像素覆盖的 y-up 区间列表 [(y0,y1),...]（合并相邻）。"""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    page = doc[0]
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    h, w = pix.height, pix.width
    doc.close()
    rows = []
    for py in range(h):
        dark = 0
        for px in range(0, w, 4):
            r, g, b = pix.pixel(px, py)[:3]
            if r < 40 and g < 40 and b < 40:
                dark += 1
                if dark > 3:
                    break
        rows.append(dark > 3)
    # 行索引 → y-up 坐标（先取 cropbox 相对高度）
    bands = []
    start = None
    for idx, is_black in enumerate(rows):
        if is_black and start is None:
            start = idx
        elif not is_black and start is not None:
            bands.append((start, idx))
            start = None
    if start is not None:
        bands.append((start, h))
    doc2 = fitz.open(stream=pdf_bytes, filetype="pdf")
    ch = doc2[0].rect.height
    doc2.close()
    # 像素 top-down → y-up：y_up = ch - (py/zoom)
    out = []
    for y0, y1 in bands:
        out.append((round(ch - y1 / zoom, 1), round(ch - y0 / zoom, 1)))
    return out


def run_case(name, crop_offset):
    data = make_pdf(crop_offset)
    # 第 2 行基线 705（文字体 705~719）→ 框 700~722
    # 第 5-10 行：第5行基线 570(顶 584) ~ 第10行基线 345(底 345) → 框 330~588
    rects = [
        {"page": 1, "x": 60, "y": 700, "w": 320, "h": 22},
        {"page": 1, "x": 60, "y": 330, "w": 320, "h": 258},
    ]
    out, report = redact(data, "rects", rects, {})
    bands = black_bands(out)
    print(f"[{name}] 黑块 y-up 区间: {bands}")
    print(f"[{name}] 期望: 第2行 ≈ (700,722)；第5-10行 ≈ (330,588)；行基线: 第5行=570 第10行=345")
    print(f"[{name}] 报告: rasterized={report['rasterizedPages']} verified={report['verified']}")
    print()
    return bands


if __name__ == "__main__":
    run_case("A 普通页面", (0, 0))
    run_case("B CropBox偏移(50,30)", (50, 30))
