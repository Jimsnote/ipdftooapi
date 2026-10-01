"""OCR 上传预检：大小/页数/PDF文字层/图片尺寸/清晰度。返回 (ok, message, has_text_layer)。"""
import os

import fitz

MAX_SIZE = 20 * 1024 * 1024  # 20MB
MAX_PAGES = 10
MIN_IMG_DIM = 200
BLURRY_VARIANCE_THRESHOLD = 100  # 拉普拉斯方差低于此值视为模糊（不阻断，仅警告）


def _check_blurriness(file_path: str) -> bool:
    """拉普拉斯方差检测模糊。返回 True=模糊。失败则返回 False（不阻断）。

    审计 L29：cv2.imread 会把整图载入内存（5000 万像素图内存尖峰数百 MB），
    用 IMREAD_REDUCED_COLOR_2 降采样读取（1/4 像素量），模糊检测是近似指标，
    降采样不影响判定用途。
    """
    try:
        import cv2

        img = cv2.imread(file_path, cv2.IMREAD_REDUCED_COLOR_2)
        if img is None:
            return False
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        variance = cv2.Laplacian(gray, cv2.CV_64F).var()
        return variance < BLURRY_VARIANCE_THRESHOLD
    except Exception:
        return False


def precheck(file_path: str, is_image: bool) -> tuple:
    """
    返回 (ok, message, has_text_layer)。
    - ok=False 时 message 为拒绝文案，has_text_layer=False。
    - ok=True 且 has_text_layer=True 时，message="has_text_layer"（调用方应走原流程/提示用户）。
    - ok=True 且 message="blurry" 时，图片较模糊（不阻断，调用方可附加警告）。
    """
    try:
        size = os.path.getsize(file_path)
    except OSError as e:
        return False, f"无法读取文件：{e}", False

    if size > MAX_SIZE:
        mb = size / 1024 / 1024
        return False, f"文件过大（{mb:.1f}MB），免费单文件限 20MB，请压缩或拆分后重试。", False

    if is_image:
        try:
            from PIL import Image

            with Image.open(file_path) as im:
                w, h = im.size
            if w < MIN_IMG_DIM or h < MIN_IMG_DIM:
                return False, f"图片分辨率过低（{w}×{h}），无法识别。建议上传 300DPI 以上扫描件。", False
        except Exception:
            pass
        # 清晰度检测（模糊不阻断，返回警告标记）
        if _check_blurriness(file_path):
            return True, "blurry", False
        return True, "", False

    # PDF
    try:
        doc = fitz.open(file_path)
    except Exception as e:
        return False, f"无法解析 PDF：{e}", False

    # 审计 #12：加密 PDF 的 get_text 会抛异常且调用点在路由 try 之外 → 裸 500，这里前置拦截
    if doc.needs_pass or doc.is_encrypted:
        doc.close()
        return False, "PDF 已加密，请先解除密码后再上传", False

    pages = len(doc)
    if pages > MAX_PAGES:
        doc.close()
        return False, f"文件 {pages} 页，免费单文件限 {MAX_PAGES} 页，请拆分后上传。", False

    # 审计 #12：单页读取异常（损坏文件等）不再向外抛出，给明确拒绝文案
    try:
        has_text = any(len(p.get_text().strip()) > 50 for p in doc)
    except Exception as e:
        doc.close()
        return False, f"无法读取 PDF 内容：{e}", False
    doc.close()
    if has_text:
        return True, "has_text_layer", True
    return True, "", False
