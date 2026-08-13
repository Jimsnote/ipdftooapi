from io import BytesIO
import os
import tempfile
import unittest
from uuid import uuid4

from fastapi import HTTPException as FastAPIHTTPException

from app.core.exceptions import InvalidFileTypeError
from app.core.file_security import get_task_dir, safe_join, save_upload_file, validate_uuid


class FakeUploadFile:
    """轻量 UploadFile 替身，仅用于 save_upload_file 测试。"""

    def __init__(self, filename: str, file: BytesIO, size: int | None = None):
        self.filename = filename
        self.file = file
        self.size = size


class FileSecurityTestCase(unittest.TestCase):
    def make_upload(self, filename: str, content: bytes) -> FakeUploadFile:
        return FakeUploadFile(filename=filename, file=BytesIO(content), size=len(content))

    def test_validate_uuid_rejects_non_uuid_task_id(self):
        with self.assertRaises(FastAPIHTTPException) as ctx:
            validate_uuid("../temp")
        self.assertEqual(ctx.exception.status_code, 400)

    def test_get_task_dir_uses_canonical_uuid_inside_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            task_id = str(uuid4())
            task_dir = get_task_dir(temp_dir, task_id)
            self.assertTrue(os.path.realpath(task_dir).startswith(os.path.realpath(temp_dir)))
            self.assertTrue(task_dir.endswith(task_id))

    def test_safe_join_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(FastAPIHTTPException) as ctx:
                safe_join(temp_dir, "..", "escape.pdf")
            self.assertEqual(ctx.exception.status_code, 400)

    def test_save_upload_file_accepts_matching_pdf_header(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            upload = self.make_upload("sample.pdf", b"%PDF-1.4\n%test")
            output = safe_join(temp_dir, "input.pdf")
            save_upload_file(upload, output, 1024, (".pdf",))
            with open(output, "rb") as f:
                self.assertTrue(f.read().startswith(b"%PDF-"))

    def test_save_upload_file_rejects_mismatched_header(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            upload = self.make_upload("fake.pdf", b"not a pdf")
            output = safe_join(temp_dir, "input.pdf")
            with self.assertRaises(InvalidFileTypeError):
                save_upload_file(upload, output, 1024, (".pdf",))


if __name__ == "__main__":
    unittest.main()
