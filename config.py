"""
config.py — PDFWala Enterprise V13.0
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).parent

_INSECURE_KEYS = {"dev-secret-change-in-prod", "changeme", "changeme-in-production-min-32-chars", ""}


class Config:
    SECRET_KEY         = os.getenv("SECRET_KEY", "dev-secret-change-in-prod")
    DEBUG              = os.getenv("DEBUG", "false").lower() == "true"
    TESTING            = False
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", 200 * 1024 * 1024))

    UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", str(BASE_DIR / "uploads"))
    OUTPUT_FOLDER = os.getenv("OUTPUT_FOLDER", str(BASE_DIR / "outputs"))
    TEMP_FOLDER   = os.getenv("TEMP_FOLDER",   str(BASE_DIR / "temp"))

    MAX_FILE_SIZE         = int(os.getenv("MAX_FILE_SIZE",         200 * 1024 * 1024))
    ASYNC_THRESHOLD       = int(os.getenv("ASYNC_THRESHOLD",        10 * 1024 * 1024))
    FILE_TTL_SEC          = int(os.getenv("FILE_TTL_SEC",           3600))
    EXCEL_ROW_LIMIT       = int(os.getenv("EXCEL_ROW_LIMIT",        10000))
    EXCEL_COL_LIMIT       = int(os.getenv("EXCEL_COL_LIMIT",        500))
    MAX_WORD_PARAGRAPHS   = int(os.getenv("MAX_WORD_PARAGRAPHS",    2000))
    MAX_WORD_TABLES       = int(os.getenv("MAX_WORD_TABLES",        100))
    MAX_PPT_SLIDES        = int(os.getenv("MAX_PPT_SLIDES",         300))
    MAX_PPT_SLIDE_ROWS    = int(os.getenv("MAX_PPT_SLIDE_ROWS",     40))
    MAX_PDF_PAGES         = int(os.getenv("MAX_PDF_PAGES",          500))
    MAX_IMAGE_DIMENSION   = int(os.getenv("MAX_IMAGE_DIMENSION",    10000))
    MAX_REMBG_BYTES       = int(os.getenv("MAX_REMBG_BYTES",        50 * 1024 * 1024))

    REDIS_URL      = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    REDIS_JOB_TTL  = int(os.getenv("REDIS_JOB_TTL", 7200))
    RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", 60))

    CELERY_BROKER_URL     = os.getenv("CELERY_BROKER_URL",     REDIS_URL)
    CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", REDIS_URL)
    QUEUE_FAST   = "fast"
    QUEUE_OFFICE = "office"
    QUEUE_SLOW   = "slow"

    GHOSTSCRIPT      = os.getenv("GHOSTSCRIPT", "gs")
    LIBREOFFICE      = os.getenv("LIBREOFFICE", "soffice")
    FFMPEG           = os.getenv("FFMPEG",      "ffmpeg")
    SIGNED_URL_SECRET = os.getenv("SIGNED_URL_SECRET", "")

    SUBPROCESS_TIMEOUT = int(os.getenv("SUBPROCESS_TIMEOUT", 120))
    PDFA_TIMEOUT       = int(os.getenv("PDFA_TIMEOUT",       180))
    OCR_TIMEOUT        = int(os.getenv("OCR_TIMEOUT",        300))
    TASK_SOFT_TIMEOUT  = int(os.getenv("TASK_SOFT_TIMEOUT",  540))
    TASK_HARD_TIMEOUT  = int(os.getenv("TASK_HARD_TIMEOUT",  600))
    OCR_WORKERS        = int(os.getenv("OCR_WORKERS",        min(4, (os.cpu_count() or 2))))
    MAX_OCR_PAGES      = int(os.getenv("MAX_OCR_PAGES",      200))

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
    def validate(cls):
        """Raise RuntimeError on insecure startup configuration."""
        _gen_cmd = "python -c \"import secrets; print(secrets.token_hex(32))\""

        if cls.SECRET_KEY in _INSECURE_KEYS or len(cls.SECRET_KEY) < 32:
            raise RuntimeError(
                "SECRET_KEY must be a random value of at least 32 characters. "
                f"Generate one with: {_gen_cmd}"
            )
        if not cls.SIGNED_URL_SECRET or len(cls.SIGNED_URL_SECRET) < 32:
            raise RuntimeError(
                "SIGNED_URL_SECRET must be a random value of at least 32 characters. "
                f"Generate one with: {_gen_cmd}"
            )
        redis_pw = os.getenv("REDIS_PASSWORD", "")
        if not redis_pw:
            raise RuntimeError(
                "REDIS_PASSWORD must be set. "
                f"Generate one with: {_gen_cmd}"
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
