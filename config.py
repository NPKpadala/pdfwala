"""
PDFWala Enterprise V11.0.0
config.py — Centralised configuration with environment overrides.
"""

import os


class Config:
    """Centralised configuration with strict environment validation."""

    VERSION = "11.0.0"

    # ========================= SECURITY (REQUIRED) =========================
    SECRET_KEY = os.environ.get("SECRET_KEY")
    if not SECRET_KEY or len(SECRET_KEY) < 32:
        raise RuntimeError(
            "SECRET_KEY is required and must be at least 32 characters long. "
            "Set it in your .env file for production security."
        )

    SIGNED_URL_SECRET = os.environ.get("SIGNED_URL_SECRET") or SECRET_KEY
    API_KEY = os.environ.get("API_KEY", "")

    # ========================= PATHS =========================
    BASE_DIR      = os.environ.get("BASE_DIR",      "/home/opc/pdfwala")
    BASE_DATA_DIR = os.environ.get("BASE_DATA_DIR", "/home/opc/pdfwala")
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "/home/opc/pdfwala/uploads")
    OUTPUT_FOLDER = os.environ.get("OUTPUT_FOLDER", "/home/opc/pdfwala/outputs")
    TEMP_FOLDER   = os.environ.get("TEMP_FOLDER",   "/home/opc/pdfwala/temp")
    STATIC_FOLDER = os.environ.get("STATIC_FOLDER", "/home/opc/pdfwala/static")

    # ========================= LIMITS =========================
    MAX_FILE_SIZE   = int(os.environ.get("MAX_FILE_SIZE",   200 * 1024 * 1024))  # 200 MB
    MAX_FILES_MERGE = int(os.environ.get("MAX_FILES_MERGE", 30))
    FILE_TTL_SEC    = int(os.environ.get("FILE_TTL_SEC",    3600))               # 1 hour
    EXCEL_ROW_LIMIT = int(os.environ.get("EXCEL_ROW_LIMIT", 5000))

    # ========================= REDIS =========================
    REDIS_URL             = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    REDIS_MAX_CONNECTIONS = int(os.environ.get("REDIS_MAX_CONNECTIONS", 50))

    # ========================= RATE LIMITING =========================
    RATE_LIMIT_FREE = int(os.environ.get("RATE_LIMIT_FREE",         100))
    RATE_LIMIT_PRO  = int(os.environ.get("RATE_LIMIT_PRO",          1000))
    RATE_LIMIT_WIN  = int(os.environ.get("RATE_LIMIT_WINDOW_SEC",   60))

    # ========================= EXTERNAL TOOLS =========================
    LIBREOFFICE  = os.environ.get("LIBREOFFICE_PATH", "soffice")
    GHOSTSCRIPT  = os.environ.get("GHOSTSCRIPT_PATH", "gs")
    WKHTMLTOPDF  = os.environ.get("WKHTMLTOPDF_PATH", "wkhtmltopdf")
    TESSERACT    = os.environ.get("TESSERACT_PATH",   "tesseract")
    VERAPDF_PATH = os.environ.get("VERAPDF_PATH",     "verapdf")

    # ========================= TIMEOUTS =========================
    PDF2WORD_TIMEOUT    = int(os.environ.get("PDF2WORD_TIMEOUT",    300))
    PDF2WORD_SYNC_LIMIT = int(os.environ.get("PDF2WORD_SYNC_LIMIT", 20 * 1024 * 1024))
    PDFA_TIMEOUT        = int(os.environ.get("PDFA_TIMEOUT",        300))
    SUBPROCESS_TIMEOUT  = int(os.environ.get("SUBPROCESS_TIMEOUT",  300))

    # ========================= SECURITY =========================
    ZIP_BOMB_RATIO  = int(os.environ.get("ZIP_BOMB_RATIO",   100))
    CORS_ORIGINS    = os.environ.get("CORS_ORIGINS",          "")   # Empty = restrictive
    SIGNED_URL_EXPIRY = int(os.environ.get("SIGNED_URL_EXPIRY", 3600))

    # ========================= MIME ALLOWLISTS =========================
    ALLOWED_PDF   = {"pdf"}
    ALLOWED_IMAGE = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tiff"}
    ALLOWED_DOC   = {"doc", "docx"}
    ALLOWED_XLS   = {"xls", "xlsx"}
    ALLOWED_HTML  = {"html", "htm"}
    ALLOWED_WEBP  = {"webp"}
    ALLOWED_PNG   = {"png"}
    ALLOWED_JPG   = {"jpg", "jpeg"}

    OLE_MAGIC = b"\xd0\xcf\x11\xe0"

    LIBRE_ALLOWED_FMTS = frozenset({
        "pdf", "docx", "xlsx", "pptx", "html", "txt", "csv", "png", "jpg"
    })

    # ========================= CIRCUIT BREAKER =========================
    CB_FAILURE_THRESHOLD = int(os.environ.get("CB_FAILURE_THRESHOLD", 5))
    CB_RECOVERY_TIMEOUT  = int(os.environ.get("CB_RECOVERY_TIMEOUT",  60))

    # ========================= FEATURE FLAGS =========================
    PDFA_VALIDATE = os.environ.get("PDFA_VALIDATE", "false").lower() in ("true", "1", "yes")

    # ========================= LOGGING =========================
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

    # ========================= CELERY BACKPRESSURE =========================
    CELERY_FAST_QUEUE_MAX   = int(os.environ.get("CELERY_FAST_QUEUE_MAX",   500))
    CELERY_OFFICE_QUEUE_MAX = int(os.environ.get("CELERY_OFFICE_QUEUE_MAX", 200))
    CELERY_SLOW_QUEUE_MAX   = int(os.environ.get("CELERY_SLOW_QUEUE_MAX",   100))

    # ========================= V11.0.0 NEW CONFIGURATION =========================

    # Job TTL in Redis (seconds) — default 2 hours
    # app.py uses: getattr(Config, 'JOB_TTL_SEC', 7200) and redis_service.client.expire(...)
    JOB_TTL_SEC = int(os.environ.get("JOB_TTL_SEC", 7200))

    # Maximum files to delete per cleanup pass (prevents I/O storms)
    # app.py uses: getattr(Config, 'CLEANUP_MAX_DELETES', 500)
    CLEANUP_MAX_DELETES = int(os.environ.get("CLEANUP_MAX_DELETES", 500))

    # Maximum slides for Word→PowerPoint conversion (prevents OOM)
    # app.py uses: getattr(Config, 'MAX_SLIDES_PPT', 200)
    MAX_SLIDES_PPT = int(os.environ.get("MAX_SLIDES_PPT", 200))

    # Graceful shutdown timeout (seconds)
    # app.py uses: getattr(Config, 'SHUTDOWN_TIMEOUT', 30)
    SHUTDOWN_TIMEOUT = int(os.environ.get("SHUTDOWN_TIMEOUT", 30))

    # Maximum concurrent OCR threads (prevents resource exhaustion)
    # app.py uses: getattr(Config, 'MAX_OCR_THREADS', 2)
    MAX_OCR_THREADS = int(os.environ.get("MAX_OCR_THREADS", 2))

    # Gunicorn worker configuration (used in gunicorn.conf.py)
    GUNICORN_WORKERS = int(os.environ.get("GUNICORN_WORKERS", 4))
    GUNICORN_THREADS = int(os.environ.get("GUNICORN_THREADS", 8))


# Create required directories on import
for _dir in [Config.UPLOAD_FOLDER, Config.OUTPUT_FOLDER, Config.TEMP_FOLDER]:
    os.makedirs(_dir, exist_ok=True)
