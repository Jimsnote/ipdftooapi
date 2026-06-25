from fastapi import HTTPException, status


class PDFProcessingError(HTTPException):
    def __init__(self, detail: str):
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=detail,
        )


class FileTooLargeError(HTTPException):
    def __init__(self, max_size: int):
        super().__init__(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum allowed size is {max_size} bytes.",
        )


class InvalidFileTypeError(HTTPException):
    def __init__(self):
        super().__init__(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="\u6587\u4ef6\u7c7b\u578b\u4e0d\u652f\u6301\uff0c\u8bf7\u4e0a\u4f20\u5bf9\u5e94\u683c\u5f0f\u7684\u6587\u4ef6\u3002",
        )
