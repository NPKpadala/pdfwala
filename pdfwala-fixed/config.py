"""
config.py — PDFWala Enterprise V13.1 (PATCHED)

FIXES:
  - Added missing SIGNED_URL_EXPIRY attribute (was AttributeError in security.py)
  - Added OUTPUT_DIR alias for OUTPUT_FOLDER (was AttributeError in pdf_engine.py)
  - Raised MAX_PDF_PAGES to 5000 (target requirement)
  - Raised MAX_OCR_PAGES to 500 and TASK timeouts for heavy workloads
  - validate() is now warn-only in dev mode, hard-fail only in production
  - ASYNC_THRESHOLD lowered to 5 MB so large files always go through Celery
  - Added MAX_IMAGE_PIXELS for decompression bomb guard
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

    # Alias used in pdf_engine._safe_output_path() — MUST match OUTPUT_FOLDER
    @classmethod
    def _get_output_dir(cls):
        return cls.OUTPUT_FOLDER

    # Resolved as a property-style class attribute via __init_subclass__ below
    # For simplicity we expose it directly:
    OUTPUT_DIR = os.getenv("OUTPUT_FOLDER", str(BASE_DIR / "outputs"))

    MAX_FILE_SIZE         = int(os.getenv("MAX_FILE_SIZE",         200 * 1024 * 1024))
    # FIX: Lower async threshold so files ≥5 MB always go to Celery workers
    ASYNC_THRESHOLD       = int(os.getenv("ASYNC_THRESHOLD",         5 * 1024 * 1024))
    FILE_TTL_SEC          = int(os.getenv("FILE_TTL_SEC",           7200))
    EXCEL_ROW_LIMIT       = int(os.getenv("EXCEL_ROW_LIMIT",        50000))
    EXCEL_COL_LIMIT       = int(os.getenv("EXCEL_COL_LIMIT",        1000))
    MAX_WORD_PARAGRAPHS   = int(os.getenv("MAX_WORD_PARAGRAPHS",    5000))
    MAX_WORD_TABLES       = int(os.getenv("MAX_WORD_TABLES",        200))
    MAX_PPT_SLIDES        = int(os.getenv("MAX_PPT_SLIDES",         500))
    MAX_PPT_SLIDE_ROWS    = int(os.getenv("MAX_PPT_SLIDE_ROWS",     100))
    # FIX: Raised from 500 to 5000 to meet the stated requirement
    MAX_PDF_PAGES         = int(os.getenv("MAX_PDF_PAGES",          5000))
    MAX_IMAGE_DIMENSION   = int(os.getenv("MAX_IMAGE_DIMENSION",    10000))
    MAX_REMBG_BYTES       = int(os.getenv("MAX_REMBG_BYTES",        50 * 1024 * 1024))
    # FIX: PIL decompression bomb guard — 200 MP (enough for 5000-page scans)
    MAX_IMAGE_PIXELS      = int(os.getenv("MAX_IMAGE_PIXELS",       200_000_000))

    REDIS_URL      = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    REDIS_JOB_TTL  = int(os.getenv("REDIS_JOB_TTL", 14400))   # 4 hours
    RATE_LIMIT_RPM = int(os.getenv("RATE_LIMIT_RPM", 120))

    CELERY_BROKER_URL     = os.getenv("CELERY_BROKER_URL",     os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", os.getenv("REDIS_URL", "redis://localhost:6379/0"))
    QUEUE_FAST   = "fast"
    QUEUE_OFFICE = "office"
    QUEUE_SLOW   = "slow"

    GHOSTSCRIPT      = os.getenv("GHOSTSCRIPT", "gs")
    LIBREOFFICE      = os.getenv("LIBREOFFICE", "soffice")
    FFMPEG           = os.getenv("FFMPEG",      "ffmpeg")
    # FIX: Provide safe defaults so startup doesn't crash in dev mode
    SIGNED_URL_SECRET = os.getenv("SIGNED_URL_SECRET", "dev-signed-url-secret-change-in-prod-min32")
    # FIX: Added missing SIGNED_URL_EXPIRY — was AttributeError every time a download URL was generated
    SIGNED_URL_EXPIRY = int(os.getenv("SIGNED_URL_EXPIRY", 3600))

    # FIX: Raised all timeouts for heavy workloads (5000 pages, 200 MB files)
    SUBPROCESS_TIMEOUT = int(os.getenv("SUBPROCESS_TIMEOUT", 300))
    PDFA_TIMEOUT       = int(os.getenv("PDFA_TIMEOUT",       600))
    OCR_TIMEOUT        = int(os.getenv("OCR_TIMEOUT",        900))
    TASK_SOFT_TIMEOUT  = int(os.getenv("TASK_SOFT_TIMEOUT",  1800))  # 30 min
    TASK_HARD_TIMEOUT  = int(os.getenv("TASK_HARD_TIMEOUT",  2100))  # 35 min
    OCR_WORKERS        = int(os.getenv("OCR_WORKERS",        min(4, (os.cpu_count() or 2))))
    # FIX: Raised OCR page limit from 200 to 500
    MAX_OCR_PAGES      = int(os.getenv("MAX_OCR_PAGES",      500))

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
        FIX: Original validate() raised RuntimeError unconditionally, blocking
        startup when env vars weren't set — this caused ALL workers to fail.
        """
        _gen_cmd = "python -c \"import secrets; print(secrets.token_hex(32))\""
        is_prod  = os.getenv("FLASK_ENV", "development") == "production"
        issues   = []

        if cls.SECRET_KEY in _INSECURE_KEYS or len(cls.SECRET_KEY) < 32:
            issues.append(
                f"SECRET_KEY is insecure. Generate with: {_gen_cmd}"
            )
        if not cls.SIGNED_URL_SECRET or len(cls.SIGNED_URL_SECRET) < 32:
            issues.append(
                f"SIGNED_URL_SECRET is weak/missing. Generate with: {_gen_cmd}"
            )

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
        # Keep OUTPUT_DIR in sync
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


CONFIG_MAP = {
    "development": DevelopmentConfig,
    "production":  ProductionConfig,
    "testing":     TestingConfig,
    "default":     DevelopmentConfig,
}


def get_config(env: str = None) -> type:
    env = env or os.getenv("FLASK_ENV", "development")
    return CONFIG_MAP.get(env, DevelopmentConfig)
