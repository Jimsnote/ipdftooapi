"""生成可搜索 PDF：原图页 + invisible 文字层（render_mode=3）。"""
import fitz


def build_searchable_pdf(image_paths, ocr_results, output_path):
    """
    image_paths: 每页图片路径列表
    ocr_results: 每页 [(bbox, text)] 列表
    output_path: 输出 PDF 路径
    """
    doc = fitz.open()
    for img_path, results in zip(image_paths, ocr_results):
        # 用图片尺寸确定页尺寸
        img_doc = fitz.open(img_path)
        page_rect = img_doc[0].rect
        page_w, page_h = page_rect.width, page_rect.height
        img_doc.close()

        page = doc.new_page(width=page_w, height=page_h)
        page.insert_image(fitz.Rect(0, 0, page_w, page_h), filename=img_path)

        # 叠加 invisible 文字层（可搜索可复制，不可见）
        for bbox, text in (results or []):
            if not text:
                continue
            # bbox: [[x1,y1],[x2,y2],[x3,y3],[x4,y4]]（图像像素坐标）
            x = bbox[0][0] if bbox and len(bbox) > 0 else 0
            y = bbox[0][1] if bbox and len(bbox) > 0 else 0
            try:
                page.insert_text(
                    (x, y + 10),
                    text,
                    fontsize=9,
                    render_mode=3,  # invisible
                    fontname="china-s",  # 简体中文
                )
            except Exception:
                # 部分字符插入失败不阻断整体
                pass

    doc.save(output_path)
    doc.close()
