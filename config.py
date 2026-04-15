import os
import secrets

class Config:
    VERSION = "10.0.0"
    SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32))
    API_KEY = os.environ.get("API_KEY", "")
    SIGNED_URL_SECRET = os.environ.get("SIGNED_URL_SECRET", SECRET_KEY)
    BASE_DATA_DIR = os.environ.get("BASE_DATA_DIR", "/data")
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", "/data/uploads")
    OUTPUT_FOLDER = os.environ.get("OUTPUT_FOLDER", "/data/outputs")
    TEMP_FOLDER = os.environ.get("TEMP_FOLDER", "/data/temp")
    MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE", 200 * 1024 * 1024))
    MAX_FILES_MERGE = int(os.environ.get("MAX_FILES_MERGE", 30))
    FILE_TTL_SEC = int(os.environ.get("FILE_TTL_SEC", 3600))
    EXCEL_ROW_LIMIT = int(os.environ.get("EXCEL_ROW_LIMIT", 5000))
    REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    REDIS_MAX_CONNECTIONS = int(os.environ.get("REDIS_MAX_CONNECTIONS", 50))
    RATE_LIMIT_FREE = int(os.environ.get("RATE_LIMIT_FREE", 100))
    RATE_LIMIT_PRO = int(os.environ.get("RATE_LIMIT_PRO", 1000))
    RATE_LIMIT_WIN = int(os.environ.get("RATE_LIMIT_WINDOW_SEC", 60))
    LIBREOFFICE = os.environ.get("LIBREOFFICE_PATH", "soffice")
    GHOSTSCRIPT = os.environ.get("GHOSTSCRIPT_PATH", "gs")
    TESSERACT = os.environ.get("TESSERACT_PATH", "tesseract")
    PDF2WORD_TIMEOUT = int(os.environ.get("PDF2WORD_TIMEOUT", 300))
    PDF2WORD_SYNC_LIMIT = int(os.environ.get("PDF2WORD_SYNC_LIMIT", 20 * 1024 * 1024))
    SUBPROCESS_TIMEOUT = int(os.environ.get("SUBPROCESS_TIMEOUT", 300))
    ZIP_BOMB_RATIO = int(os.environ.get("ZIP_BOMB_RATIO", 100))
    CORS_ORIGINS = os.environ.get("CORS_ORIGINS", "*")
    SIGNED_URL_EXPIRY = int(os.environ.get("SIGNED_URL_EXPIRY", 3600))
    LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
    CB_FAILURE_THRESHOLD = int(os.environ.get("CB_FAILURE_THRESHOLD", 5))
    CB_RECOVERY_TIMEOUT = int(os.environ.get("CB_RECOVERY_TIMEOUT", 60))
    ALLOWED_PDF = {"pdf"}
    ALLOWED_IMAGE = {"jpg","jpeg","png","webp","gif","bmp","tiff"}
    ALLOWED_DOC = {"doc","docx"}
    ALLOWED_XLS = {"xls","xlsx"}
    LIBRE_ALLOWED_FMTS = frozenset({"pdf","docx","xlsx","pptx","html","txt","csv","png","jpg"})
for d in [Config.UPLOAD_FOLDER, Config.OUTPUT_FOLDER, Config.TEMP_FOLDER]:
    os.makedirs(d, exist_ok=True)
