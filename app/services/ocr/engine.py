"""OCR engine wrapper: PaddleOCR PP-OCRv6 small, lazy singleton.

Engine selectable via OCR_ENGINE env:
  - "onnxruntime" (default, Linux production, fast)
  - "mkldnn_false" (Windows local test, paddle_static + enable_mkldnn=False)
"""
import os
import json
import threading

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "0")

_lock = threading.Lock()
_ocr = None


def _build():
    from paddleocr import PaddleOCR

    engine = os.environ.get("OCR_ENGINE", "onnxruntime")
    common = dict(
        use_doc_orientation_classify=False,
        use_textline_orientation=False,
        use_doc_unwarping=False,
        text_detection_model_name="PP-OCRv6_small_det",
        text_recognition_model_name="PP-OCRv6_small_rec",
    )
    if engine == "onnxruntime":
        return PaddleOCR(engine="onnxruntime", **common)
    return PaddleOCR(enable_mkldnn=False, **common)


def get_ocr():
    global _ocr
    if _ocr is None:
        with _lock:
            if _ocr is None:
                _ocr = _build()
    return _ocr


def _extract_texts_polys(obj):
    """递归找 rec_texts / dt_polys。"""
    if isinstance(obj, dict):
        t = obj.get("rec_texts")
        p = obj.get("dt_polys")
        if t is not None:
            return t, p
        for v in obj.values():
            r = _extract_texts_polys(v)
            if r[0] is not None:
                return r
    return None, None


def ocr_image(img_path: str):
    """对一张图跑 OCR，返回 [(bbox, text), ...]，bbox 为 4 点坐标列表。"""
    ocr = get_ocr()
    res = list(ocr.predict(img_path))
    if not res:
        return []
    r = res[0]
    j = getattr(r, "json", None)
    data = json.loads(j) if isinstance(j, str) else (j if isinstance(j, dict) else {})
    texts, polys = _extract_texts_polys(data)
    out = []
    for i, t in enumerate(texts or []):
        bbox = polys[i] if polys and i < len(polys) else None
        out.append((bbox, t))
    return out
