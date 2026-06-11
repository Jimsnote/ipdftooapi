import base64
import os
from typing import Tuple

from easyofd.ofd import OFD

from app.core.logger import get_logger

logger = get_logger(__name__)


class OFDConverter:
    """OFD 版式文档转换服务（基于 easyofd）"""

    def __init__(self):
        self._ofd = OFD()

    def ofd_to_pdf(self, input_path: str, output_path: str) -> Tuple[bool, str]:
        """
        将 OFD 文件转换为 PDF。

        :param input_path: 输入 OFD 文件路径
        :param output_path: 输出 PDF 文件路径
        :return: (是否成功, 结果消息)
        """
        try:
            with open(input_path, "rb") as f:
                ofd_b64 = str(base64.b64encode(f.read()), "utf-8")

            self._ofd.read(ofd_b64, save_xml=False)
            pdf_bytes = self._ofd.to_pdf()
            self._ofd.del_data()

            with open(output_path, "wb") as f:
                f.write(pdf_bytes)

            logger.info(f"OFD 转 PDF 成功: {input_path} -> {output_path}")
            return True, output_path

        except Exception as e:
            logger.error(f"OFD 转 PDF 失败: {e}")
            # 确保内存释放
            try:
                self._ofd.del_data()
            except Exception:
                pass
            return False, str(e)

    def ofd_to_images(self, input_path: str, output_dir: str) -> Tuple[bool, list]:
        """
        将 OFD 文件逐页转为图片（用于预览）。

        :param input_path: 输入 OFD 文件路径
        :param output_dir: 图片输出目录
        :return: (是否成功, 图片路径列表)
        """
        try:
            with open(input_path, "rb") as f:
                ofd_b64 = str(base64.b64encode(f.read()), "utf-8")

            self._ofd.read(ofd_b64, save_xml=False)
            img_list = self._ofd.to_jpg()
            self._ofd.del_data()

            saved_paths = []
            for idx, img in enumerate(img_list):
                from PIL import Image

                im = Image.fromarray(img)
                path = os.path.join(output_dir, f"page_{idx + 1}.jpg")
                im.save(path, quality=95)
                saved_paths.append(path)

            logger.info(f"OFD 转图片成功: {len(saved_paths)} 页")
            return True, saved_paths

        except Exception as e:
            logger.error(f"OFD 转图片失败: {e}")
            try:
                self._ofd.del_data()
            except Exception:
                pass
            return False, []
