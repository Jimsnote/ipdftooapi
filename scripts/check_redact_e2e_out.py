"""e2e 输出检查：第 3 页黑块落点像素定位（v2：大面积全黑行判定）。"""
import sys

import fitz

OUT = "C:/Users/jim_z/AppData/Local/Temp/redact_e2e_out.pdf"
PAGE_H = 841.89

doc = fitz.open(OUT)
print("页数:", len(doc))
page = doc[2]  # 第 3 页
text = page.get_text()
kept = [i for i in range(1, 16) if f"LINE-{i:02d}" in text]
print("第 3 页保留行:", kept, "（期望 1,3,4,11,12,13,14,15）")
header = "PAGE-3" in text
print("页眉 PAGE-3 保留:", header)

# 像素：找大面积纯黑带（涂黑块），文字行采样点大多为白，阈值 80%
zoom = 1.0
pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
h, w = pix.height, pix.width
step = 4
total = w // step
rows = []
for py in range(h):
    dark = sum(
        1
        for px in range(0, w, step)
        if (lambda p: p[0] < 40 and p[1] < 40 and p[2] < 40)(pix.pixel(px, py))
    )
    rows.append(dark >= total * 0.8)

bands = []
start = None
for idx, b in enumerate(rows):
    if b and start is None:
        start = idx
    elif not b and start is not None:
        if idx - start > 3:
            bands.append((start, idx))
        start = None
if start is not None:
    bands.append((start, h))

ch = page.rect.height
bands_yup = [(round(ch - y1 / zoom, 1), round(ch - y0 / zoom, 1)) for y0, y1 in bands]
doc.close()

print("黑块 y-up 区间:", bands_yup)
print("期望: 框1 ≈ (700,722)；框2 ≈ (330,588)")

ok1 = any(abs(a - 700) < 8 and abs(b - 722) < 8 for a, b in bands_yup)
ok2 = any(abs(a - 330) < 8 and abs(b - 588) < 8 for a, b in bands_yup)
text_ok = kept == [1, 3, 4, 11, 12, 13, 14, 15]
print(f"框1 落点: {'PASS' if ok1 else 'FAIL'}")
print(f"框2 落点: {'PASS' if ok2 else 'FAIL'}")
print(f"文本删除: {'PASS' if text_ok else 'FAIL'}")
print("总体:", "PASS" if (ok1 and ok2 and text_ok) else "FAIL")
sys.exit(0 if (ok1 and ok2 and text_ok) else 1)
