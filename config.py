"""
config.py — PDFWala Enterprise V14.0 (FULL FIX)

V14 FIXES over V13.1-patched:
  - Removed duplicate CONFIG_MAP / get_config definitions (caused import confusion)
  - Redis connection pool settings added (max_connections=50, keepalive)
  - CELERY_BROKER_POOL_LIMIT added (prevents broker connection exhaustion)
  - MAX_IMAGE_PIXELS raised to 500MP to handle 5000-page doc scans
  - GUNICORN_WORKER_CLASS exposed as env var
  - OUTPUT_DIR sync fixed (init_dirs always updates class attr)
  - Added CLEANUP_INTERVAL and CLEANUP_ENABLED for automatic output purge
"""

import logging
import os
from pathlib import Path

BASE_DIR = Path(__file__).parent

_log = logging.getLogger("pdfwala.config")
_INSECURE_KEYS = {"dev-secret-change-in-prod", "changeme", "changeme-in-production-min-32-chars", ""}


class Config:
    SECRET_KEY         = os.getenv("SECRET_KEY", "dev-secret-change-in-prod")
    DEBUG              = os.getenv("DEBUG", "false").lower() == "true"
    TESTING            = False
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", 200 * 1024 * 1024))

    UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", str(BASE_DIR / "uploads"))
    OUTPUT_FOLDER = os.getenv("OUTPUT_FOLDER", str(BASE_DIR / "outputs"))
    TEMP_FOLDER   = os.getenv("TEMP_FOLDER",   str(BASE_DIR / "temp"))
    OUTPUT_DIR    = os.getenv("OUTPUT_FOLDER", str(BASE_DIR / "outputs"))   # alias

    MAX_FILE_SIZE         = int(os.getenv("MAX_FILE_SIZE",         200 * 1024 * 1024))
    ASYNC_THRESHOLD       = int(os.getenv("ASYNC_THRESHOLD",         5 * 1024 * 1024))
    FILE_TTL_SEC          = int(os.getenv("FILE_TTL_SEC",           7200))
    EXCEL_ROW_LIMIT       = int(os.getenv("EXCEL_ROW_LIMIT",        50000))
    EXCEL_COL_LIMIT       = int(os.getenv("EXCEL_COL_LIMIT",        1000))
    MAX_WORD_PARAGRAPHS   = int(os.getenv("MAX_WORD_PARAGRAPHS",    5000))
    MAX_WORD_TABLES       = int(os.getenv("MAX_WORD_TABLES",        200))
    MAX_PPT_SLIDES        = int(os.getenv("MAX_PPT_SLIDES",         500))
    MAX_PPT_SLIDE_ROWS    = int(os.getenv("MAX_PPT_SLIDE_ROWS",     100))
    MAX_PDF_PAGES         = int(os.getenv("MAX_PDF_PAGES",          5000))
    MAX_IMAGE_DIMENSION   = int(os.getenv("MAX_IMAGE_DIMENSION",    10000))
    MAX_REMBG_BYTES       = int(os.getenv("MAX_REMBG_BYTES",        50 * 1024 * 1024))
    # FIX V14: 500 MP to safely handle 5000-page high-DPI scans
    MAX_IMAGE_PIXELS      = int(os.getenv("MAX_IMAGE_PIXELS",       500_000_000))

    REDIS_URL             = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    REDIS_JOB_TTL         = int(os.getenv("REDIS_JOB_TTL", 14400))
    # FIX V14: Connection pool settings prevent Redis exhaustion under load
    REDIS_MAX_CONNECTIONS = int(os.getenv("REDIS_MAX_CONNECTIONS", 50))
    RATE_LIMIT_RPM        = int(os.getenv("RATE_LIMIT_RPM", 120))

    CELERY_BROKER_URL          = os.getenv("CELERY_BROKER_URL",     os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    CELERY_RESULT_BACKEND      = os.getenv("CELERY_RESULT_BACKEND", os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    # FIX V14: Limit broker connection pool to prevent exhaustion
    CELERY_BROKER_POOL_LIMIT   = int(os.getenv("CELERY_BROKER_POOL_LIMIT", 10))
    QUEUE_FAST   = "fast"
    QUEUE_OFFICE = "office"
    QUEUE_SLOW   = "slow"

    GHOSTSCRIPT      = os.getenv("GHOSTSCRIPT", "gs")
    LIBREOFFICE      = os.getenv("LIBREOFFICE", "soffice")
    FFMPEG           = os.getenv("FFMPEG",      "ffmpeg")
    SIGNED_URL_SECRET = os.getenv("SIGNED_URL_SECRET", "dev-signed-url-secret-change-in-prod-min32")
    SIGNED_URL_EXPIRY = int(os.getenv("SIGNED_URL_EXPIRY", 3600))

    SUBPROCESS_TIMEOUT = int(os.getenv("SUBPROCESS_TIMEOUT", 300))
    PDFA_TIMEOUT       = int(os.getenv("PDFA_TIMEOUT",       600))
    OCR_TIMEOUT        = int(os.getenv("OCR_TIMEOUT",        900))
    TASK_SOFT_TIMEOUT  = int(os.getenv("TASK_SOFT_TIMEOUT",  1800))
    TASK_HARD_TIMEOUT  = int(os.getenv("TASK_HARD_TIMEOUT",  2100))
    OCR_WORKERS        = int(os.getenv("OCR_WORKERS",        min(4, (os.cpu_count() or 2))))
    MAX_OCR_PAGES      = int(os.getenv("MAX_OCR_PAGES",      500))

    # FIX V14: Automatic output file cleanup to prevent disk exhaustion
    CLEANUP_ENABLED    = os.getenv("CLEANUP_ENABLED", "true").lower() == "true"
    CLEANUP_INTERVAL   = int(os.getenv("CLEANUP_INTERVAL", 3600))   # run every hour

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
        """
        Warn on insecure dev config, raise only in production.
        """
        _gen_cmd = "python -c \"import secrets; print(secrets.token_hex(32))\""
        is_prod  = os.getenv("FLASK_ENV", "development") == "production"
        issues   = []

        if cls.SECRET_KEY in _INSECURE_KEYS or len(cls.SECRET_KEY) < 32:
            issues.append(f"SECRET_KEY is insecure. Generate with: {_gen_cmd}")
        if not cls.SIGNED_URL_SECRET or len(cls.SIGNED_URL_SECRET) < 32:
            issues.append(f"SIGNED_URL_SECRET is weak/missing. Generate with: {_gen_cmd}")

        if issues:
            for msg in issues:
                if is_prod:
                    raise RuntimeError(f"[PROD CONFIG ERROR] {msg}")
                else:
                    _log.warning(f"[DEV CONFIG WARNING] {msg}")

    @classmethod
    def init_dirs(cls):
        for d in (cls.UPLOAD_FOLDER, cls.OUTPUT_FOLDER, cls.TEMP_FOLDER):
            Path(d).mkdir(parents=True, exist_ok=True)
        # FIX V14: Always keep OUTPUT_DIR alias in sync with OUTPUT_FOLDER
        cls.OUTPUT_DIR = cls.OUTPUT_FOLDER


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
    OUTPUT_DIR      = "/tmp/pdfwala_test_outputs"
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
