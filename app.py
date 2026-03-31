"""
PDFWala - Production-Hardened Backend (V3.0 - Full Feature Set)
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
import math
from contextlib import contextmanager
from functools import wraps
from datetime import datetime
from flask import Flask, request, jsonify, send_file, send_from_directory, g
from flask_cors import CORS
from werkzeug.utils import secure_filename

# PDF Libraries
from PyPDF2 import PdfReader, PdfWriter, PdfMerger
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.colors import Color
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
        b"RIFF":         "image/webp",  # WEBP starts with RIFF
    }
    ALLOWED_PDF_EXT   = {"pdf"}
    ALLOWED_IMAGE_EXT = {"jpg", "jpeg", "png", "webp"}
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
    header = file_obj.read(12)  # Read more bytes to handle WEBP (RIFF....WEBP)
    file_obj.seek(0)
    # Special WEBP check: RIFF????WEBP
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    for magic, mime in Config.MAGIC_BYTES.items():
        if magic != b"RIFF" and header.startswith(magic):
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
# HELPER: Generate watermark PDF page
# ─────────────────────────────────────────────────────────────────
def _create_watermark_pdf(text: str, opacity: float, color_hex: str, page_width: float, page_height: float) -> bytes:
    """Creates an in-memory PDF page with a diagonal text watermark."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_width, page_height))

    # Parse hex color (default grey)
    try:
        color_hex = color_hex.lstrip("#")
        r = int(color_hex[0:2], 16) / 255
        g_val = int(color_hex[2:4], 16) / 255
        b = int(color_hex[4:6], 16) / 255
    except Exception:
        r, g_val, b = 0.5, 0.5, 0.5

    c.setFillColor(Color(r, g_val, b, alpha=opacity))
    c.setFont("Helvetica-Bold", 48)
    c.saveState()
    c.translate(page_width / 2, page_height / 2)
    c.rotate(45)
    c.drawCentredString(0, 0, text)
    c.restoreState()
    c.save()
    buf.seek(0)
    return buf.read()

# ─────────────────────────────────────────────────────────────────
# HELPER: Generate page number overlay PDF page
# ─────────────────────────────────────────────────────────────────
def _create_page_number_pdf(page_num: int, position: str, page_width: float, page_height: float) -> bytes:
    """Creates an in-memory PDF page with a page number."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_width, page_height))
    c.setFont("Helvetica", 12)
    c.setFillColor(Color(0, 0, 0, alpha=1))
    label = str(page_num)
    x = page_width / 2
    y = 20 if position == "bottom" else page_height - 30
    c.drawCentredString(x, y, label)
    c.save()
    buf.seek(0)
    return buf.read()

# ─────────────────────────────────────────────────────────────────
# API ENDPOINTS — EXISTING
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

# ─────────────────────────────────────────────────────────────────
# 1. /api/image-to-pdf
#    Accept multiple images (JPG, PNG, WEBP), convert to single PDF
# ─────────────────────────────────────────────────────────────────
@app.route("/api/image-to-pdf", methods=["POST"])
@rate_limited()
def image_to_pdf():
    files = request.files.getlist("files")
    if not files or len(files) == 0:
        return err("No image files provided")
    if len(files) > Config.MAX_FILES_MERGE:
        return err(f"Too many files. Max allowed: {Config.MAX_FILES_MERGE}")

    # Validate all files first
    for f in files:
        verr = validate_file(f, Config.ALLOWED_IMAGE_EXT)
        if verr:
            return err(f"Invalid file '{f.filename}': {verr}")

    try:
        with temp_uploads(files) as paths:
            out = output_path("converted.pdf")
            pil_images = []

            for p in paths:
                try:
                    img = Image.open(p)
                    # Convert to RGB (handles RGBA, P, L modes for PDF compatibility)
                    if img.mode in ("RGBA", "P", "LA"):
                        background = Image.new("RGB", img.size, (255, 255, 255))
                        if img.mode == "P":
                            img = img.convert("RGBA")
                        background.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
                        img = background
                    elif img.mode != "RGB":
                        img = img.convert("RGB")
                    pil_images.append(img)
                except UnidentifiedImageError:
                    return err(f"Cannot process image: {os.path.basename(p)}")

            if not pil_images:
                return err("No valid images to convert")

            # Save first image as base, append remaining as additional pages
            first = pil_images[0]
            rest  = pil_images[1:]
            first.save(
                out,
                format="PDF",
                save_all=True,
                append_images=rest,
                resolution=150
            )

        return ok(f"Converted {len(pil_images)} image(s) to PDF", path=out)
    except Exception as e:
        log.error(f"image-to-pdf error: {e}")
        return err("Conversion failed", 500)

# ─────────────────────────────────────────────────────────────────
# 2. /api/rotate
#    Rotate PDF pages (90, 180, 270). Support specific pages or all.
# ─────────────────────────────────────────────────────────────────
@app.route("/api/rotate", methods=["POST"])
@rate_limited()
def rotate_pdf():
    f = request.files.get("file")
    angle_str = request.form.get("angle", "90")
    pages_str = request.form.get("pages", "all")  # "all" or "1,3,5" or "2-4"

    verr = validate_file(f, Config.ALLOWED_PDF_EXT)
    if verr: return err(verr)

    try:
        angle = int(angle_str)
        if angle not in (90, 180, 270):
            return err("Angle must be 90, 180, or 270")
    except ValueError:
        return err("Invalid angle value")

    try:
        with temp_upload(f) as path:
            reader = PdfReader(path)
            writer = PdfWriter()
            total = len(reader.pages)

            # Parse target page indices (0-based)
            if pages_str.strip().lower() == "all":
                target_pages = set(range(total))
            else:
                target_pages = set()
                for part in pages_str.split(","):
                    part = part.strip()
                    if "-" in part:
                        s, e = map(int, part.split("-"))
                        target_pages.update(range(s - 1, e))
                    else:
                        target_pages.add(int(part) - 1)

            for i, page in enumerate(reader.pages):
                if i in target_pages:
                    page.rotate(angle)
                writer.add_page(page)

            out = output_path("rotated.pdf")
            with open(out, "wb") as fh:
                writer.write(fh)

        return ok(f"Rotated {len(target_pages)} page(s) by {angle}°", path=out)
    except Exception as e:
        log.error(f"rotate error: {e}")
        return err("Rotation failed", 500)

# ─────────────────────────────────────────────────────────────────
# 3. /api/watermark
#    Add diagonal text watermark to all pages (reportlab + PyPDF2)
# ─────────────────────────────────────────────────────────────────
@app.route("/api/watermark", methods=["POST"])
@rate_limited()
def watermark_pdf():
    f        = request.files.get("file")
    text     = request.form.get("text", "CONFIDENTIAL").strip()
    opacity  = float(request.form.get("opacity", "0.3"))
    color    = request.form.get("color", "808080")  # hex without #

    verr = validate_file(f, Config.ALLOWED_PDF_EXT)
    if verr: return err(verr)

    if not text:
        return err("Watermark text cannot be empty")
    opacity = max(0.05, min(1.0, opacity))  # clamp 0.05–1.0

    try:
        with temp_upload(f) as path:
            reader = PdfReader(path)
            writer = PdfWriter()

            for page in reader.pages:
                # Get page dimensions from mediabox
                mb = page.mediabox
                pw = float(mb.width)
                ph = float(mb.height)

                # Build watermark overlay for this page's dimensions
                wm_bytes = _create_watermark_pdf(text, opacity, color, pw, ph)
                wm_reader = PdfReader(io.BytesIO(wm_bytes))
                wm_page   = wm_reader.pages[0]

                # Merge watermark onto the content page
                page.merge_page(wm_page)
                writer.add_page(page)

            out = output_path("watermarked.pdf")
            with open(out, "wb") as fh:
                writer.write(fh)

        return ok("Watermark applied", path=out)
    except Exception as e:
        log.error(f"watermark error: {e}")
        return err("Watermarking failed", 500)

# ─────────────────────────────────────────────────────────────────
# 4. /api/protect
#    Password-protect a PDF (AES-256 via PyPDF2)
# ─────────────────────────────────────────────────────────────────
@app.route("/api/protect", methods=["POST"])
@rate_limited()
def protect_pdf():
    f        = request.files.get("file")
    password = request.form.get("password", "").strip()

    verr = validate_file(f, Config.ALLOWED_PDF_EXT)
    if verr: return err(verr)

    if not password:
        return err("Password is required")
    if len(password) > 128:
        return err("Password too long (max 128 chars)")

    try:
        with temp_upload(f) as path:
            reader = PdfReader(path)

            # Refuse to double-encrypt an already-encrypted PDF
            if reader.is_encrypted:
                return err("PDF is already password-protected")

            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)

            writer.encrypt(password, algorithm="AES-256")

            out = output_path("protected.pdf")
            with open(out, "wb") as fh:
                writer.write(fh)

        return ok("PDF protected with password", path=out)
    except Exception as e:
        log.error(f"protect error: {e}")
        return err("Protection failed", 500)

# ─────────────────────────────────────────────────────────────────
# 5. /api/unlock
#    Remove password from a PDF after validating it
# ─────────────────────────────────────────────────────────────────
@app.route("/api/unlock", methods=["POST"])
@rate_limited()
def unlock_pdf():
    f        = request.files.get("file")
    password = request.form.get("password", "").strip()

    verr = validate_file(f, Config.ALLOWED_PDF_EXT)
    if verr: return err(verr)

    if not password:
        return err("Password is required")

    try:
        with temp_upload(f) as path:
            reader = PdfReader(path)

            if not reader.is_encrypted:
                return err("PDF is not password-protected")

            if not reader.decrypt(password):
                return err("Incorrect password", 401)

            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)

            out = output_path("unlocked.pdf")
            with open(out, "wb") as fh:
                writer.write(fh)

        return ok("PDF unlocked successfully", path=out)
    except Exception as e:
        log.error(f"unlock error: {e}")
        return err("Unlock failed", 500)

# ─────────────────────────────────────────────────────────────────
# 6. /api/page-numbers
#    Stamp page numbers on every page (reportlab overlay + PyPDF2)
# ─────────────────────────────────────────────────────────────────
@app.route("/api/page-numbers", methods=["POST"])
@rate_limited()
def page_numbers_pdf():
    f            = request.files.get("file")
    position     = request.form.get("position", "bottom").lower()
    start_number = int(request.form.get("start", "1"))

    verr = validate_file(f, Config.ALLOWED_PDF_EXT)
    if verr: return err(verr)

    if position not in ("top", "bottom"):
        return err("Position must be 'top' or 'bottom'")

    try:
        with temp_upload(f) as path:
            reader = PdfReader(path)
            writer = PdfWriter()

            for i, page in enumerate(reader.pages):
                mb = page.mediabox
                pw = float(mb.width)
                ph = float(mb.height)

                pn_bytes  = _create_page_number_pdf(start_number + i, position, pw, ph)
                pn_reader = PdfReader(io.BytesIO(pn_bytes))
                pn_page   = pn_reader.pages[0]

                page.merge_page(pn_page)
                writer.add_page(page)

            out = output_path("numbered.pdf")
            with open(out, "wb") as fh:
                writer.write(fh)

        return ok(f"Page numbers added starting from {start_number}", path=out)
    except Exception as e:
        log.error(f"page-numbers error: {e}")
        return err("Adding page numbers failed", 500)

# ─────────────────────────────────────────────────────────────────
# 7. /api/organize
#    Reorder, extract, or delete pages via a comma-separated order list
#    e.g. "3,1,2" → output has original pages 3, 1, 2 (1-indexed)
# ─────────────────────────────────────────────────────────────────
@app.route("/api/organize", methods=["POST"])
@rate_limited()
def organize_pdf():
    f     = request.files.get("file")
    order = request.form.get("order", "").strip()  # e.g. "3,1,2" or "1-3,5"

    verr = validate_file(f, Config.ALLOWED_PDF_EXT)
    if verr: return err(verr)

    if not order:
        return err("'order' parameter is required (e.g. '3,1,2' or '1-3,5')")

    try:
        with temp_upload(f) as path:
            reader     = PdfReader(path)
            total      = len(reader.pages)
            page_indices = []

            for part in order.split(","):
                part = part.strip()
                if "-" in part:
                    s, e = map(int, part.split("-"))
                    page_indices.extend(range(s - 1, e))
                else:
                    page_indices.append(int(part) - 1)

            # Validate indices
            invalid = [i + 1 for i in page_indices if i < 0 or i >= total]
            if invalid:
                return err(f"Page number(s) out of range: {invalid}. PDF has {total} pages.")

            writer = PdfWriter()
            for idx in page_indices:
                writer.add_page(reader.pages[idx])

            out = output_path("organized.pdf")
            with open(out, "wb") as fh:
                writer.write(fh)

        return ok(f"Organized PDF with {len(page_indices)} page(s)", path=out)
    except Exception as e:
        log.error(f"organize error: {e}")
        return err("Organize failed", 500)

# ─────────────────────────────────────────────────────────────────
# 8. /api/crop
#    Crop PDF pages by adjusting the mediabox with margin offsets
#    Accepts: top, bottom, left, right (in points, 1 pt ≈ 0.353 mm)
# ─────────────────────────────────────────────────────────────────
@app.route("/api/crop", methods=["POST"])
@rate_limited()
def crop_pdf():
    f = request.files.get("file")

    verr = validate_file(f, Config.ALLOWED_PDF_EXT)
    if verr: return err(verr)

    try:
        top    = float(request.form.get("top",    0))
        bottom = float(request.form.get("bottom", 0))
        left   = float(request.form.get("left",   0))
        right  = float(request.form.get("right",  0))
    except ValueError:
        return err("Margin values must be numeric (points)")

    if any(v < 0 for v in (top, bottom, left, right)):
        return err("Margin values must be non-negative")

    try:
        with temp_upload(f) as path:
            reader = PdfReader(path)
            writer = PdfWriter()

            for page in reader.pages:
                mb = page.mediabox
                x0 = float(mb.left)
                y0 = float(mb.bottom)
                x1 = float(mb.right)
                y1 = float(mb.top)

                new_x0 = x0 + left
                new_y0 = y0 + bottom
                new_x1 = x1 - right
                new_y1 = y1 - top

                if new_x0 >= new_x1 or new_y0 >= new_y1:
                    return err("Crop margins exceed page dimensions")

                page.mediabox.left   = new_x0
                page.mediabox.bottom = new_y0
                page.mediabox.right  = new_x1
                page.mediabox.top    = new_y1
                # Also crop cropbox to match so viewers respect the crop
                page.cropbox.left   = new_x0
                page.cropbox.bottom = new_y0
                page.cropbox.right  = new_x1
                page.cropbox.top    = new_y1

                writer.add_page(page)

            out = output_path("cropped.pdf")
            with open(out, "wb") as fh:
                writer.write(fh)

        return ok("PDF cropped successfully", path=out)
    except Exception as e:
        log.error(f"crop error: {e}")
        return err("Crop failed", 500)

# ─────────────────────────────────────────────────────────────────
# 9. /api/info
#    Extract metadata: pages, file size, author, title, etc.
#    Returns JSON (no file download)
# ─────────────────────────────────────────────────────────────────
@app.route("/api/info", methods=["POST"])
@rate_limited()
def pdf_info():
    f = request.files.get("file")

    verr = validate_file(f, Config.ALLOWED_PDF_EXT)
    if verr: return err(verr)

    try:
        with temp_upload(f) as path:
            file_size = os.path.getsize(path)
            reader    = PdfReader(path)

            if reader.is_encrypted:
                return jsonify({
                    "success": True,
                    "encrypted": True,
                    "message": "PDF is encrypted. Provide password to inspect further.",
                    "file_size_bytes": file_size,
                    "size_human": f"{file_size / 1024:.1f} KB" if file_size < 1_048_576 else f"{file_size / 1_048_576:.2f} MB"
                })

            meta  = reader.metadata or {}
            total = len(reader.pages)

            # Gather per-page dimensions
            page_sizes = []
            for i, page in enumerate(reader.pages):
                mb = page.mediabox
                page_sizes.append({
                    "page": i + 1,
                    "width_pt":  round(float(mb.width),  2),
                    "height_pt": round(float(mb.height), 2),
                })

            info = {
                "success":          True,
                "encrypted":        False,
                "page_count":       total,
                "file_size_bytes":  file_size,
                "size_human":       f"{file_size / 1024:.1f} KB" if file_size < 1_048_576 else f"{file_size / 1_048_576:.2f} MB",
                "title":            meta.get("/Title",    None),
                "author":           meta.get("/Author",   None),
                "subject":          meta.get("/Subject",  None),
                "creator":          meta.get("/Creator",  None),
                "producer":         meta.get("/Producer", None),
                "creation_date":    str(meta.get("/CreationDate", None)),
                "modification_date":str(meta.get("/ModDate",      None)),
                "page_sizes":       page_sizes,
            }

        return jsonify(info)
    except Exception as e:
        log.error(f"info error: {e}")
        return err("Could not extract PDF info", 500)

# ─────────────────────────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────────────────────────
@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "app": "PDFWala", "version": "3.0.0"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
