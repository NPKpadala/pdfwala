"""
PDFWala - Production-Hardened Backend (V2.1 - Fitz/PyMuPDF Patch)
Author: PDFWala Team
Security: Magic-byte validation, rate limiting, safe cleanup, structured logging
"""

import os
import io
import uuid
import zipfile
import logging
import hashlib
import time
import threading
import shutil
from contextlib import contextmanager
from functools import wraps
from datetime import datetime
from flask import Flask, request, jsonify, send_file, send_from_directory, g
from flask_cors import CORS
from werkzeug.utils import secure_filename

# PDF Libraries
from PyPDF2 import PdfReader, PdfWriter, PdfMerger
from reportlab.pdfgen import canvas
from PIL import Image, UnidentifiedImageError
import fitz  # PyMuPDF

# ─────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────
class Config:
    UPLOAD_FOLDER   = os.environ.get("UPLOAD_FOLDER",  "/tmp/pdfwala/uploads")
    OUTPUT_FOLDER   = os.environ.get("OUTPUT_FOLDER",  "/tmp/pdfwala/outputs")
    MAX_FILE_SIZE   = int(os.environ.get("MAX_FILE_SIZE", 100 * 1024 * 1024))   # 100 MB
    MAX_FILES_MERGE = int(os.environ.get("MAX_FILES_MERGE", 20))
    FILE_TTL_SEC    = int(os.environ.get("FILE_TTL_SEC", 3600))                  # 1 hr
    RATE_LIMIT      = int(os.environ.get("RATE_LIMIT", 30))                      # req/min
    SECRET_KEY      = os.environ.get("SECRET_KEY", uuid.uuid4().hex)

    MAGIC_BYTES = {
        b"%PDF":         "application/pdf",
        b"\xff\xd8\xff": "image/jpeg",
        b"\x89PNG\r\n":  "image/png",
        b"GIF87a":       "image/gif",
        b"GIF89a":       "image/gif",
    }
    ALLOWED_PDF_EXT   = {"pdf"}
    ALLOWED_IMAGE_EXT = {"jpg", "jpeg", "png"}
    ALLOWED_ALL_EXT   = ALLOWED_PDF_EXT | ALLOWED_IMAGE_EXT

for folder in [Config.UPLOAD_FOLDER, Config.OUTPUT_FOLDER]:
    os.makedirs(folder, exist_ok=True)

# ─────────────────────────────────────────────────────────────────
# APP INIT
# ─────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = Config.MAX_FILE_SIZE
app.secret_key = Config.SECRET_KEY
CORS(app, resources={r"/api/*": {"origins": os.environ.get("ALLOWED_ORIGINS", "*")}})

# ─────────────────────────────────────────────────────────────────
# STRUCTURED LOGGING
# ─────────────────────────────────────────────────────────────────
class JSONFormatter(logging.Formatter):
    def format(self, record):
        import json
        log = {
            "ts":      datetime.utcnow().isoformat(),
            "level":    record.levelname,
            "logger":   record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log["exc"] = self.formatException(record.exc_info)
        return json.dumps(log)

handler = logging.StreamHandler()
handler.setFormatter(JSONFormatter())
logging.root.handlers = [handler]
logging.root.setLevel(logging.INFO)
log = logging.getLogger("pdfwala")

# ─────────────────────────────────────────────────────────────────
# IN-MEMORY RATE LIMITER
# ─────────────────────────────────────────────────────────────────
_rate_store: dict = {}
_rate_lock  = threading.Lock()

def _get_client_ip() -> str:
    xff = request.headers.get("X-Forwarded-For")
    return xff.split(",")[0].strip() if xff else (request.remote_addr or "unknown")

def rate_limited(per_minute: int = Config.RATE_LIMIT):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            ip  = _get_client_ip()
            now = time.monotonic()
            window = 60.0
            with _rate_lock:
                hits = _rate_store.get(ip, [])
                hits = [t for t in hits if now - t < window]
                if len(hits) >= per_minute:
                    retry_after = int(window - (now - hits[0])) + 1
                    resp = jsonify({"success": False, "message": "Rate limit exceeded", "retry_after": retry_after})
                    resp.status_code = 429
                    resp.headers["Retry-After"] = str(retry_after)
                    return resp
                hits.append(now)
                _rate_store[ip] = hits
            return fn(*args, **kwargs)
        return wrapper
    return decorator

# ─────────────────────────────────────────────────────────────────
# SECURITY HEADERS
# ─────────────────────────────────────────────────────────────────
@app.after_request
def secure_headers(response):
    response.headers["X-Content-Type-Options"]  = "nosniff"
    response.headers["X-Frame-Options"]          = "DENY"
    response.headers["X-XSS-Protection"]         = "1; mode=block"
    response.headers["Referrer-Policy"]           = "strict-origin-when-cross-origin"
    response.headers.pop("Server", None)
    return response

@app.before_request
def start_timer():
    g.start = time.time()

@app.after_request
def log_request(response):
    duration = round((time.time() - g.get("start", time.time())) * 1000, 2)
    log.info({
        "method": request.method,
        "path":   request.path,
        "status": response.status_code,
        "ip":     _get_client_ip(),
        "ms":     duration,
    })
    return response

# ─────────────────────────────────────────────────────────────────
# FILE VALIDATION
# ─────────────────────────────────────────────────────────────────
def _detect_mime(file_obj) -> str | None:
    header = file_obj.read(8)
    file_obj.seek(0)
    for magic, mime in Config.MAGIC_BYTES.items():
        if header.startswith(magic):
            return mime
    return None

def validate_file(file, allowed_ext: set, max_bytes: int = Config.MAX_FILE_SIZE) -> str | None:
    if not file or file.filename == "":
        return "No file provided"
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in allowed_ext:
        return f"Extension '{ext}' not allowed."
    mime = _detect_mime(file)
    if mime is None:
        return "File type invalid"
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > max_bytes:
        return "File too large"
    return None

# ─────────────────────────────────────────────────────────────────
# CONTEXT MANAGERS
# ─────────────────────────────────────────────────────────────────
@contextmanager
def temp_upload(file):
    ext  = file.filename.rsplit(".", 1)[-1].lower()
    name = f"{uuid.uuid4()}.{ext}"
    path = os.path.join(Config.UPLOAD_FOLDER, name)
    try:
        file.save(path)
        yield path
    finally:
        if os.path.exists(path):
            os.remove(path)

@contextmanager
def temp_uploads(files):
    paths = []
    try:
        for f in files:
            ext  = f.filename.rsplit(".", 1)[-1].lower()
            path = os.path.join(Config.UPLOAD_FOLDER, f"{uuid.uuid4()}.{ext}")
            f.save(path)
            paths.append(path)
        yield paths
    finally:
        for p in paths:
            if os.path.exists(p): os.remove(p)

def output_path(suffix: str) -> str:
    return os.path.join(Config.OUTPUT_FOLDER, f"{uuid.uuid4()}_{secure_filename(suffix)}")

def err(msg: str, code: int = 400):
    return jsonify({"success": False, "message": msg}), code

def ok(msg: str, path: str = None, **extras):
    payload = {"success": True, "message": msg, **extras}
    if path:
        size = os.path.getsize(path)
        payload["download_url"] = f"/download/{os.path.basename(path)}"
        payload["size_human"]   = f"{size / 1024:.1f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.2f} MB"
    return jsonify(payload)

# ─────────────────────────────────────────────────────────────────
# API ENDPOINTS
# ─────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return send_from_directory("static", "index.html")

@app.route("/download/<filename>")
def download(filename):
    path = os.path.join(Config.OUTPUT_FOLDER, secure_filename(filename))
    if not os.path.exists(path): return err("File not found", 404)
    return send_file(path, as_attachment=True)

@app.route("/api/merge", methods=["POST"])
@rate_limited()
def merge_pdf():
    files = request.files.getlist("files")
    if len(files) < 2: return err("Min 2 files")
    try:
        with temp_uploads(files) as paths:
            merger = PdfMerger()
            for p in paths: merger.append(p)
            out = output_path("merged.pdf")
            merger.write(out)
            merger.close()
        return ok("Merged", path=out)
    except Exception: return err("Merge failed", 500)

@app.route("/api/compress", methods=["POST"])
@rate_limited()
def compress_pdf():
    f = request.files.get("file")
    quality = request.form.get("quality", "medium").lower()
    q_val = {"low": 30, "medium": 50, "high": 75}.get(quality, 50)
    try:
        with temp_upload(f) as path:
            orig_size = os.path.getsize(path)
            doc = fitz.open(path)
            out = output_path("compressed.pdf")
            for page in doc:
                for img in page.get_images(full=True):
                    xref = img[0]
                    try:
                        base = doc.extract_image(xref)
                        pil = Image.open(io.BytesIO(base["image"])).convert("RGB")
                        buf = io.BytesIO()
                        pil.save(buf, format="JPEG", quality=q_val, optimize=True)
                        doc.update_stream(xref, buf.getvalue())
                    except: pass
            doc.save(out, deflate=True, garbage=4)
            new_size = os.path.getsize(out)
            doc.close()
            reduction = round((1 - new_size / orig_size) * 100, 1) if orig_size else 0
        return ok("Compressed", path=out, reduction_pct=reduction)
    except Exception: return err("Compression failed", 500)

@app.route("/api/pdf-to-image", methods=["POST"])
@rate_limited()
def pdf_to_image():
    f = request.files.get("file")
    fmt = request.form.get("format", "png").lower()
    dpi = min(int(request.form.get("dpi", 150)), 300)
    try:
        with temp_upload(f) as path:
            doc = fitz.open(path)
            page_count = len(doc)
            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for i, page in enumerate(doc):
                    pix = page.get_pixmap(matrix=fitz.Matrix(dpi/72, dpi/72))
                    zf.writestr(f"page_{i+1:03d}.{fmt}", pix.tobytes(fmt))
            doc.close()
            out = output_path("images.zip")
            with open(out, "wb") as fh: fh.write(zip_buf.getvalue())
        return ok(f"Exported {page_count} images", path=out)
    except Exception: return err("Export failed", 500)

@app.route("/api/split", methods=["POST"])
@rate_limited()
def split_pdf():
    f = request.files.get("file")
    mode = request.form.get("mode", "all") 
    ranges = request.form.get("ranges", "")
    
    try:
        validation_err = validate_file(f, Config.ALLOWED_PDF_EXT)
        if validation_err: return err(validation_err)

        with temp_upload(f) as path:
            reader = PdfReader(path)
            total_pages = len(reader.pages)
            
            pages_to_keep = []
            if mode == "all":
                pages_to_keep = list(range(total_pages))
            else:
                for part in ranges.split(','):
                    part = part.strip()
                    if '-' in part:
                        start, end = map(int, part.split('-'))
                        pages_to_keep.extend(range(start-1, end))
                    else:
                        pages_to_keep.append(int(part) - 1)

            out = output_path("split_pages.zip")
            zip_buf = io.BytesIO()
            
            with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for idx in pages_to_keep:
                    if 0 <= idx < total_pages:
                        writer = PdfWriter()
                        writer.add_page(reader.pages[idx])
                        page_io = io.BytesIO()
                        writer.write(page_io)
                        zf.writestr(f"page_{idx+1}.pdf", page_io.getvalue())

            with open(out, "wb") as fh:
                fh.write(zip_buf.getvalue())
                
        return ok(f"Split into {len(pages_to_keep)} files", path=out)
    except Exception as e:
        log.error(f"Split error: {str(e)}")
        return err("Split failed", 500)

@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "app": "PDFWala", "version": "2.1.0"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
