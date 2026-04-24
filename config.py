"""
PDFWala Enterprise V11.0.0
config.py — Centralised configuration with environment overrides and validation.
"""

import os
from pathlib import Path


class Config:
    """Centralised configuration with strict environment validation."""

    VERSION = "11.0.0"

    # =========================================================================
    # SECURITY (REQUIRED — WILL FAIL FAST IF MISSING)
    # =========================================================================
    
    SECRET_KEY = os.environ.get("SECRET_KEY")
    if not SECRET_KEY or len(SECRET_KEY) < 32:
        raise RuntimeError(
            "SECRET_KEY is required and must be at least 32 characters long. "
            "Set it in your .env file or environment variables."
        )

    SIGNED_URL_SECRET = os.environ.get("SIGNED_URL_SECRET") or SECRET_KEY
    API_KEY = os.environ.get("API_KEY", "")

    # =========================================================================
    # FILESYSTEM PATHS
    # =========================================================================
    
    BASE_DIR = os.environ.get("BASE_DIR", "/home/opc/pdfwala")
    BASE_DATA_DIR = os.environ.get("BASE_DATA_DIR", "/home/opc/pdfwala")
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "/home/opc/pdfwala/uploads")
    OUTPUT_FOLDER = os.environ.get("OUTPUT_FOLDER", "/home/opc/pdfwala/outputs")
    TEMP_FOLDER = os.environ.get("TEMP_FOLDER", "/home/opc/pdfwala/temp")
    STATIC_FOLDER = os.environ.get("STATIC_FOLDER", "/home/opc/pdfwala/static")

    # =========================================================================
    # FILE SIZE & COUNT LIMITS
    # =========================================================================
    
    MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE", 200 * 1024 * 1024))  # 200 MB
    MAX_FILES_MERGE = int(os.environ.get("MAX_FILES_MERGE", 30))
    FILE_TTL_SEC = int(os.environ.get("FILE_TTL_SEC", 3600))  # 1 hour
    EXCEL_ROW_LIMIT = int(os.environ.get("EXCEL_ROW_LIMIT", 5000))

    # =========================================================================
    # REDIS CONFIGURATION
    # =========================================================================
    
    REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    REDIS_MAX_CONNECTIONS = int(os.environ.get("REDIS_MAX_CONNECTIONS", 50))

    # =========================================================================
    # RATE LIMITING
    # =========================================================================
    
    RATE_LIMIT_FREE = int(os.environ.get("RATE_LIMIT_FREE", 100))
    RATE_LIMIT_PRO = int(os.environ.get("RATE_LIMIT_PRO", 1000))
    RATE_LIMIT_WINDOW_SEC = int(os.environ.get("RATE_LIMIT_WINDOW_SEC", 60))

    # =========================================================================
    # EXTERNAL TOOL PATHS
    # =========================================================================
    
    LIBREOFFICE = os.environ.get("LIBREOFFICE_PATH", "soffice")
    GHOSTSCRIPT = os.environ.get("GHOSTSCRIPT_PATH", "gs")
    WKHTMLTOPDF = os.environ.get("WKHTMLTOPDF_PATH", "wkhtmltopdf")
    TESSERACT = os.environ.get("TESSERACT_PATH", "tesseract")
    VERAPDF_PATH = os.environ.get("VERAPDF_PATH", "verapdf")

    # =========================================================================
    # OPERATION TIMEOUTS (SECONDS)
    # =========================================================================
    
    PDF2WORD_TIMEOUT = int(os.environ.get("PDF2WORD_TIMEOUT", 300))
    PDF2WORD_SYNC_LIMIT = int(os.environ.get("PDF2WORD_SYNC_LIMIT", 20 * 1024 * 1024))
    PDFA_TIMEOUT = int(os.environ.get("PDFA_TIMEOUT", 300))
    SUBPROCESS_TIMEOUT = int(os.environ.get("SUBPROCESS_TIMEOUT", 300))

    # =========================================================================
    # SECURITY HARDENING
    # =========================================================================
    
    ZIP_BOMB_RATIO = int(os.environ.get("ZIP_BOMB_RATIO", 100))
    CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "")  # Empty = restrictive (same-origin only)
    SIGNED_URL_EXPIRY = int(os.environ.get("SIGNED_URL_EXPIRY", 3600))

    # =========================================================================
    # FILE EXTENSION ALLOWLISTS
    # =========================================================================
    
    ALLOWED_PDF = {"pdf"}
    ALLOWED_IMAGE = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tiff"}
    ALLOWED_DOC = {"doc", "docx"}
    ALLOWED_XLS = {"xls", "xlsx"}
    ALLOWED_HTML = {"html", "htm"}
    ALLOWED_WEBP = {"webp"}
    ALLOWED_PNG = {"png"}
    ALLOWED_JPG = {"jpg", "jpeg"}

    # Magic bytes for OLE files (legacy Office formats)
    OLE_MAGIC = b"\xd0\xcf\x11\xe0"

    # Output formats supported by LibreOffice converter
    LIBRE_ALLOWED_FMTS = frozenset({
        "pdf", "docx", "xlsx", "pptx", "html", "txt", "csv", "png", "jpg"
    })

    # =========================================================================
    # CIRCUIT BREAKER SETTINGS
    # =========================================================================
    
    CB_FAILURE_THRESHOLD = int(os.environ.get("CB_FAILURE_THRESHOLD", 5))
    CB_RECOVERY_TIMEOUT = int(os.environ.get("CB_RECOVERY_TIMEOUT", 60))

    # =========================================================================
    # FEATURE FLAGS
    # =========================================================================
    
    PDFA_VALIDATE = os.environ.get("PDFA_VALIDATE", "false").lower() in ("true", "1", "yes")

    # =========================================================================
    # LOGGING
    # =========================================================================
    
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

    # =========================================================================
    # CELERY QUEUE BACKPRESSURE LIMITS
    # =========================================================================
    
    CELERY_FAST_QUEUE_MAX = int(os.environ.get("CELERY_FAST_QUEUE_MAX", 500))
    CELERY_OFFICE_QUEUE_MAX = int(os.environ.get("CELERY_OFFICE_QUEUE_MAX", 200))
    CELERY_SLOW_QUEUE_MAX = int(os.environ.get("CELERY_SLOW_QUEUE_MAX", 100))

    # =========================================================================
    # V11.0.0 — PRODUCTION HARDENING CONFIGURATION
    # =========================================================================

    # Redis job TTL (seconds) — prevents orphaned job entries
    JOB_TTL_SEC = int(os.environ.get("JOB_TTL_SEC", 7200))  # 2 hours

    # Maximum files to delete per cleanup pass — prevents I/O storms
    CLEANUP_MAX_DELETES = int(os.environ.get("CLEANUP_MAX_DELETES", 500))

    # Maximum slides for Word → PowerPoint conversion — prevents OOM
    MAX_SLIDES_PPT = int(os.environ.get("MAX_SLIDES_PPT", 200))

    # Graceful shutdown timeout (seconds) — allows in-flight requests to complete
    SHUTDOWN_TIMEOUT = int(os.environ.get("SHUTDOWN_TIMEOUT", 30))

    # Maximum concurrent OCR threads — prevents Tesseract resource exhaustion
    MAX_OCR_THREADS = int(os.environ.get("MAX_OCR_THREADS", 2))

    # =========================================================================
    # V11.1.0 — CHUNKED PARALLEL PROCESSING CONFIGURATION
    # =========================================================================

    # ── OCR (Tesseract: ~200 MB RAM / worker) ─────────────────────────────────
    OCR_CHUNK_THRESHOLD = int(os.environ.get("OCR_CHUNK_THRESHOLD", 50))
    OCR_CHUNK_PAGES     = int(os.environ.get("OCR_CHUNK_PAGES",     30))
    OCR_MAX_WORKERS     = int(os.environ.get("OCR_MAX_WORKERS",      2))

    # ── PDF → Excel (pdfplumber: moderate CPU + RAM) ──────────────────────────
    PDF_TO_EXCEL_CHUNK_THRESHOLD = int(os.environ.get("PDF_TO_EXCEL_CHUNK_THRESHOLD", 80))
    PDF_TO_EXCEL_CHUNK_PAGES     = int(os.environ.get("PDF_TO_EXCEL_CHUNK_PAGES",     80))
    PDF_TO_EXCEL_MAX_WORKERS     = int(os.environ.get("PDF_TO_EXCEL_MAX_WORKERS",      2))

    # ── Watermark (light stamp op) ────────────────────────────────────────────
    WATERMARK_CHUNK_THRESHOLD    = int(os.environ.get("WATERMARK_CHUNK_THRESHOLD",    200))
    WATERMARK_CHUNK_PAGES        = int(os.environ.get("WATERMARK_CHUNK_PAGES",        100))
    WATERMARK_MAX_WORKERS        = int(os.environ.get("WATERMARK_MAX_WORKERS",          4))

    # ── Rotate (trivial PyMuPDF op) ───────────────────────────────────────────
    ROTATE_CHUNK_THRESHOLD       = int(os.environ.get("ROTATE_CHUNK_THRESHOLD",       200))
    ROTATE_CHUNK_PAGES           = int(os.environ.get("ROTATE_CHUNK_PAGES",           100))
    ROTATE_MAX_WORKERS           = int(os.environ.get("ROTATE_MAX_WORKERS",             4))

    # ── Page Numbers (light overlay op) ──────────────────────────────────────
    PAGE_NUMBERS_CHUNK_THRESHOLD = int(os.environ.get("PAGE_NUMBERS_CHUNK_THRESHOLD", 200))
    PAGE_NUMBERS_CHUNK_PAGES     = int(os.environ.get("PAGE_NUMBERS_CHUNK_PAGES",     100))
    PAGE_NUMBERS_MAX_WORKERS     = int(os.environ.get("PAGE_NUMBERS_MAX_WORKERS",       4))

    # ── Redact (text search: moderate CPU) ────────────────────────────────────
    REDACT_CHUNK_THRESHOLD       = int(os.environ.get("REDACT_CHUNK_THRESHOLD",        50))
    REDACT_CHUNK_PAGES           = int(os.environ.get("REDACT_CHUNK_PAGES",            50))
    REDACT_MAX_WORKERS           = int(os.environ.get("REDACT_MAX_WORKERS",             2))

    # ── PDF → Image (rasterize: fast per-page) ────────────────────────────────
    PDF_TO_IMAGE_CHUNK_THRESHOLD = int(os.environ.get("PDF_TO_IMAGE_CHUNK_THRESHOLD",  50))
    PDF_TO_IMAGE_CHUNK_PAGES     = int(os.environ.get("PDF_TO_IMAGE_CHUNK_PAGES",      50))
    PDF_TO_IMAGE_MAX_WORKERS     = int(os.environ.get("PDF_TO_IMAGE_MAX_WORKERS",       4))

    # ── Crop (trivial cropbox set) ────────────────────────────────────────────
    CROP_CHUNK_THRESHOLD         = int(os.environ.get("CROP_CHUNK_THRESHOLD",         200))
    CROP_CHUNK_PAGES             = int(os.environ.get("CROP_CHUNK_PAGES",             100))
    CROP_MAX_WORKERS             = int(os.environ.get("CROP_MAX_WORKERS",               4))

    # ── Per-tool page guards (hard caps — return 413 over these) ─────────────
    MAX_WATERMARK_PAGES          = int(os.environ.get("MAX_WATERMARK_PAGES",   1000))
    MAX_ROTATE_PAGES             = int(os.environ.get("MAX_ROTATE_PAGES",      1000))
    MAX_PAGE_NUMBERS_PAGES       = int(os.environ.get("MAX_PAGE_NUMBERS_PAGES",1000))
    MAX_REDACT_PAGES             = int(os.environ.get("MAX_REDACT_PAGES",       500))
    MAX_CROP_PAGES               = int(os.environ.get("MAX_CROP_PAGES",        1000))

    # Maximum pages to compare in compare-pdf endpoint — prevents OOM
    MAX_COMPARE_PAGES = int(os.environ.get("MAX_COMPARE_PAGES", 50))

    # Maximum pages for synchronous OCR — larger files must use async endpoint
    MAX_OCR_PAGES_SYNC = int(os.environ.get("MAX_OCR_PAGES_SYNC", 30))

    # Maximum pages to convert to images — prevents OOM
    MAX_IMAGE_PAGES = int(os.environ.get("MAX_IMAGE_PAGES", 50))

    # PIL/Pillow decompression bomb threshold — prevents pixel explosion attacks
    MAX_IMAGE_PIXELS = int(os.environ.get("MAX_IMAGE_PIXELS", 89_478_485))

    # Gunicorn worker configuration
    GUNICORN_WORKERS = int(os.environ.get("GUNICORN_WORKERS", 4))
    GUNICORN_THREADS = int(os.environ.get("GUNICORN_THREADS", 8))


# =============================================================================
# STARTUP DIRECTORY CREATION
# =============================================================================

for _dir in [Config.UPLOAD_FOLDER, Config.OUTPUT_FOLDER, Config.TEMP_FOLDER]:
    os.makedirs(_dir, exist_ok=True)
