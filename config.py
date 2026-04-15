"""
PDFWala Enterprise V10.0
config.py — Centralised configuration with environment overrides.
"""

import os
import secrets


class Config:
    """Centralised configuration with environment overrides."""

    VERSION        = "10.0.0"
    SECRET_KEY     = os.environ.get("SECRET_KEY",      secrets.token_hex(32))
    API_KEY        = os.environ.get("API_KEY",         "")
    SIGNED_URL_SECRET = os.environ.get("SIGNED_URL_SECRET", SECRET_KEY)

    # ── Paths ──────────────────────────────────────────────────────────────────
    BASE_DIR       = os.environ.get("BASE_DIR",        "/app")
    BASE_DATA_DIR  = os.environ.get("BASE_DATA_DIR",   "/data")
    UPLOAD_FOLDER  = os.environ.get("UPLOAD_FOLDER",   "/data/uploads")
    OUTPUT_FOLDER  = os.environ.get("OUTPUT_FOLDER",   "/data/outputs")
    TEMP_FOLDER    = os.environ.get("TEMP_FOLDER",     "/data/temp")
    STATIC_FOLDER  = os.environ.get("STATIC_FOLDER",   "/app/static")

    # ── Limits ─────────────────────────────────────────────────────────────────
    MAX_FILE_SIZE   = int(os.environ.get("MAX_FILE_SIZE",   200 * 1024 * 1024))
    MAX_FILES_MERGE = int(os.environ.get("MAX_FILES_MERGE", 30))
    FILE_TTL_SEC    = int(os.environ.get("FILE_TTL_SEC",    3600))
    EXCEL_ROW_LIMIT = int(os.environ.get("EXCEL_ROW_LIMIT", 5000))

    # ── Redis ──────────────────────────────────────────────────────────────────
    REDIS_URL             = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    REDIS_MAX_CONNECTIONS = int(os.environ.get("REDIS_MAX_CONNECTIONS", 50))

    # ── Rate limiting ──────────────────────────────────────────────────────────
    RATE_LIMIT_FREE = int(os.environ.get("RATE_LIMIT_FREE", 100))
    RATE_LIMIT_PRO  = int(os.environ.get("RATE_LIMIT_PRO",  1000))
    RATE_LIMIT_WIN  = int(os.environ.get("RATE_LIMIT_WINDOW_SEC", 60))

    # ── External tools ─────────────────────────────────────────────────────────
    LIBREOFFICE  = os.environ.get("LIBREOFFICE_PATH", "soffice")
    GHOSTSCRIPT  = os.environ.get("GHOSTSCRIPT_PATH", "gs")
    WKHTMLTOPDF  = os.environ.get("WKHTMLTOPDF_PATH", "wkhtmltopdf")
    TESSERACT    = os.environ.get("TESSERACT_PATH",   "tesseract")

    # ── Timeouts (seconds) ─────────────────────────────────────────────────────
    PDF2WORD_TIMEOUT    = int(os.environ.get("PDF2WORD_TIMEOUT",    300))
    PDF2WORD_SYNC_LIMIT = int(os.environ.get("PDF2WORD_SYNC_LIMIT", 20 * 1024 * 1024))
    PDFA_TIMEOUT        = int(os.environ.get("PDFA_TIMEOUT",        300))
    SUBPROCESS_TIMEOUT  = int(os.environ.get("SUBPROCESS_TIMEOUT",  300))

    # ── Security ───────────────────────────────────────────────────────────────
    ZIP_BOMB_RATIO    = int(os.environ.get("ZIP_BOMB_RATIO",    100))
    CORS_ORIGINS      = os.environ.get("CORS_ORIGINS",          "*")
    SIGNED_URL_EXPIRY = int(os.environ.get("SIGNED_URL_EXPIRY", 3600))

    # ── MIME / Extension allowlists ────────────────────────────────────────────
    ALLOWED_PDF   = {"pdf"}
    ALLOWED_IMAGE = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tiff"}
    ALLOWED_DOC   = {"doc", "docx"}
    ALLOWED_XLS   = {"xls", "xlsx"}
    ALLOWED_HTML  = {"html", "htm"}
    ALLOWED_WEBP  = {"webp"}
    ALLOWED_PNG   = {"png"}
    ALLOWED_JPG   = {"jpg", "jpeg"}

    OLE_MAGIC = b"\xd0\xcf\x11\xe0"

    # LibreOffice output format allowlist
    LIBRE_ALLOWED_FMTS = frozenset({
        "pdf", "docx", "xlsx", "pptx", "html", "txt", "csv", "png", "jpg"
    })

    # Circuit breaker thresholds
    CB_FAILURE_THRESHOLD = int(os.environ.get("CB_FAILURE_THRESHOLD", 5))
    CB_RECOVERY_TIMEOUT  = int(os.environ.get("CB_RECOVERY_TIMEOUT",  60))

    # PDF/A optional validation
    PDFA_VALIDATE = os.environ.get("PDFA_VALIDATE", "false").lower() in ("true", "1", "yes")
    VERAPDF_PATH  = os.environ.get("VERAPDF_PATH", "verapdf")

    # Log level
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

    # Celery queues
    CELERY_FAST_QUEUE_MAX   = int(os.environ.get("CELERY_FAST_QUEUE_MAX",   500))
    CELERY_OFFICE_QUEUE_MAX = int(os.environ.get("CELERY_OFFICE_QUEUE_MAX", 200))
    CELERY_SLOW_QUEUE_MAX   = int(os.environ.get("CELERY_SLOW_QUEUE_MAX",   100))
