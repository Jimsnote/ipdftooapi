import base64
import os
import traceback
from typing import Tuple

from easyofd.ofd import OFD

from app.core.logger import get_logger

logger = get_logger(__name__)


def _patch_easyofd_signature_assert() -> None:
    """
    修复 easyofd==20260427 对「有签名值但无骑缝章」OFD 的崩溃缺陷。

    缺陷链路（easyofd/parser_ofd/ofd_parser.py）：
        1. SignatureFileParser.__call__ 只有在 XML 里找到 <ofd:StampAnnot>
           时才填充结果 dict，否则返回空 dict {}；
        2. ofd_parser.py 第 ~340 行随后执行
               SignedValue = signatures_info.get("SignedValue")   # -> None
               self.get_xml_obj(SignedValue)                       # -> assert label 抛错
        3. get_xml_obj 开头是裸断言 `assert label`，对 None 抛无消息
           AssertionError()，调用方 str(e) 得到空串，前端只显示
           “OFD 转换失败: ”（一片空白），无法排障。

    触发条件：OFD 含 <ofd:Signatures>，且 Signature.xml 只有
    <ofd:SignedInfo>/<ofd:SignedValue> 而没有 <ofd:StampAnnot>。
    铁路 12306 电子发票（Provider=ChinaRailway12306）等即属此类。

    修复：给 get_xml_obj 加空值保护——label 为空时返回 ""，这与该函数
    「找不到就返回空串」的既有契约一致，签名信息缺失不影响版式转换结果。
    """
    try:
        from easyofd.parser_ofd.ofd_parser import OFDParser
    except Exception as e:  # pragma: no cover - 依赖缺失时不应阻断应用启动
        logger.warning(f"easyofd OFDParser 导入失败，跳过签名兼容补丁: {e}")
        return

    if getattr(OFDParser.get_xml_obj, "_ipdftoo_patched", False):
        return

    original_get_xml_obj = OFDParser.get_xml_obj

    def get_xml_obj_safe(self, label):
        if not label:
            return ""
        return original_get_xml_obj(self, label)

    get_xml_obj_safe._ipdftoo_patched = True
    OFDParser.get_xml_obj = get_xml_obj_safe
    logger.info("已应用 easyofd 签名兼容补丁（无 StampAnnot 的 SignedValue 空值保护）")


_patch_easyofd_signature_assert()


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
            # 记录完整堆栈：easyofd 会抛无消息异常（如裸 assert），
            # 只记 str(e) 会得到空串，线上将无法排障。
            logger.error(f"OFD 转 PDF 失败: {e!r}\n{traceback.format_exc()}")
            # 确保内存释放
            try:
                self._ofd.del_data()
            except Exception:
                pass
            # str(e) 可能为空串，回退到异常类名，保证调用方能拿到可读信息
            return False, str(e) or type(e).__name__

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
            logger.error(f"OFD 转图片失败: {e!r}\n{traceback.format_exc()}")
            try:
                self._ofd.del_data()
            except Exception:
                pass
            return False, []
