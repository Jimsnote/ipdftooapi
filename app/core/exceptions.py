from fastapi import HTTPException, status


class PDFProcessingError(HTTPException):
    def __init__(self, detail: str):
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=detail,
        )


class FileTooLargeError(HTTPException):
    def __init__(self, max_size: int):
        # 审计 P3 英文错误族：413 文案中文化（此前直出英文，全部上传端点共用）
        super().__init__(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"文件过大，最大支持 {max_size // (1024 * 1024)}MB，请压缩或拆分后重试",
        )


class InvalidFileTypeError(HTTPException):
    def __init__(self):
        super().__init__(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="\u6587\u4ef6\u7c7b\u578b\u4e0d\u652f\u6301\uff0c\u8bf7\u4e0a\u4f20\u5bf9\u5e94\u683c\u5f0f\u7684\u6587\u4ef6\u3002",
        )
