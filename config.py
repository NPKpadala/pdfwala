"""
config.py — PDFWala Enterprise V13.0
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).parent


class Config:
    SECRET_KEY         = os.getenv("SECRET_KEY", "dev-secret-change-in-prod")
    DEBUG              = os.getenv("DEBUG", "false").lower() == "true"
    TESTING            = False
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", 200 * 1024 * 1024))

    UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", str(BASE_DIR / "uploads"))
    OUTPUT_FOLDER = os.getenv("OUTPUT_FOLDER", str(BASE_DIR / "outputs"))
    TEMP_FOLDER   = os.getenv("TEMP_FOLDER",   str(BASE_DIR / "temp"))

    MAX_FILE_SIZE    = int(os.getenv("MAX_FILE_SIZE",    200 * 1024 * 1024))
    ASYNC_THRESHOLD  = int(os.getenv("ASYNC_THRESHOLD",  10 * 1024 * 1024))
    FILE_TTL_SEC     = int(os.getenv("FILE_TTL_SEC",     3600))
    EXCEL_ROW_LIMIT  = int(os.getenv("EXCEL_ROW_LIMIT",  10000))

    REDIS_URL      = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    REDIS_JOB_TTL  = int(os.getenv("REDIS_JOB_TTL", 7200))
    RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", 60))

    CELERY_BROKER_URL     = os.getenv("CELERY_BROKER_URL",     REDIS_URL)
    CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL)
    QUEUE_FAST   = "fast"
    QUEUE_OFFICE = "office"
    QUEUE_SLOW   = "slow"

    GHOSTSCRIPT = os.getenv("GHOSTSCRIPT", "gs")
    LIBREOFFICE = os.getenv("LIBREOFFICE", "libreoffice")
    FFMPEG      = os.getenv("FFMPEG",      "ffmpeg")

    SUBPROCESS_TIMEOUT = int(os.getenv("SUBPROCESS_TIMEOUT", 120))
    PDFA_TIMEOUT       = int(os.getenv("PDFA_TIMEOUT",       180))
    OCR_TIMEOUT        = int(os.getenv("OCR_TIMEOUT",        300))
    TASK_SOFT_TIMEOUT  = int(os.getenv("TASK_SOFT_TIMEOUT",  540))
    TASK_HARD_TIMEOUT  = int(os.getenv("TASK_HARD_TIMEOUT",  600))

    LIBRE_ALLOWED_FMTS = {
        "pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt",
        "txt", "html", "csv", "png", "jpg",
    }

    CORS_ORIGINS    = os.getenv("CORS_ORIGINS", "*").split(",")
    API_KEY_HEADER  = "X-API-Key"
    REQUIRE_API_KEY = os.getenv("REQUIRE_API_KEY", "false").lower() == "true"
    VALID_API_KEYS  = set(
        k.strip() for k in os.getenv("API_KEYS", "").split(",") if k.strip()
    )

    @classmethod
    def init_dirs(cls):
        for d in (cls.UPLOAD_FOLDER, cls.OUTPUT_FOLDER, cls.TEMP_FOLDER):
            Path(d).mkdir(parents=True, exist_ok=True)


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False


class TestingConfig(Config):
    TESTING         = True
    DEBUG           = True
    REDIS_URL       = os.getenv("TEST_REDIS_URL", "redis://localhost:6379/15")
    UPLOAD_FOLDER   = "/tmp/pdfwala_test_uploads"
    OUTPUT_FOLDER   = "/tmp/pdfwala_test_outputs"
    TEMP_FOLDER     = "/tmp/pdfwala_test_temp"
    ASYNC_THRESHOLD = 999 * 1024 * 1024


CONFIG_MAP = {
    "development": DevelopmentConfig,
    "production":  ProductionConfig,
    "testing":     TestingConfig,
    "default":     DevelopmentConfig,
}


def get_config(env: str = None) -> type:
    env = env or os.getenv("FLASK_ENV", "development")
    return CONFIG_MAP.get(env, DevelopmentConfig)
