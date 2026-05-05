"""
core/exceptions.py — PDFWala Enterprise V12.0
All custom exceptions for clean error propagation across all layers.

Exception hierarchy:
  PDFWalaError
    ├── ValidationError        (400)  bad input / file type / size
    ├── ProcessingError        (500)  engine failed mid-run
    ├── UnsupportedOperation   (501)  library not installed
    ├── ResourceError          (503)  disk / memory exhausted
    └── TimeoutError           (408)  subprocess or task timed out
"""


class PDFWalaError(Exception):
    """Base for all PDFWala exceptions. Always carries an http_code."""

    http_code: int = 500

    def __init__(self, message: str, http_code: int = None):
        super().__init__(message)
        self.message = message
        if http_code is not None:
            self.http_code = http_code

    def to_dict(self) -> dict:
        return {"success": False, "error": self.message}


class ValidationError(PDFWalaError):
    """Bad user input — wrong file type, too large, empty PDF, bad params."""
    http_code = 400

    def __init__(self, message: str):
        super().__init__(message, 400)


class ProcessingError(PDFWalaError):
    """Engine-level failure — conversion failed, corrupt output, etc."""
    http_code = 500

    def __init__(self, message: str, cause: Exception = None):
        super().__init__(message, 500)
        self.cause = cause


class UnsupportedOperation(PDFWalaError):
    """Required library not installed."""
    http_code = 501

    def __init__(self, operation: str, library: str):
        super().__init__(
            f"{operation} requires {library} which is not installed.", 501
        )


class ResourceError(PDFWalaError):
    """Disk full, memory exhausted, or file system error."""
    http_code = 503


class OperationTimeoutError(PDFWalaError):
    """Subprocess or Celery task timed out."""
    http_code = 408

    def __init__(self, operation: str, timeout_seconds: int):
        super().__init__(
            f"{operation} timed out after {timeout_seconds}s.", 408
        )
