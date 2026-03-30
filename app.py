"""
PDFWala - Production-Hardened Backend
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
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file, send_from_directory, g
from flask_cors import CORS
from werkzeug.utils import secure_filename

# PDF Libraries
from PyPDF2 import PdfReader, PdfWriter, PdfMerger
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter, A4
from reportlab.lib import colors
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

    # Magic bytes → allowed MIME
    MAGIC_BYTES = {
        b"%PDF":        "application/pdf",
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
            "level":   record.levelname,
            "logger":  record.name,
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
# IN-MEMORY RATE LIMITER  (drop-in, no Redis required for MVP)
# ─────────────────────────────────────────────────────────────────
_rate_store: dict = {}
_rate_lock  = threading.Lock()

def _get_client_ip() -> str:
    """Respect X-Forwarded-For from trusted proxy."""
    xff = request.headers.get("X-Forwarded-For")
    return xff.split(",")[0].strip() if xff else (request.remote_addr or "unknown")

def rate_limited(per_minute: int = Config.RATE_LIMIT):
    """Sliding-window rate limiter decorator."""
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
    response.headers["Permissions-Policy"]        = "geolocation=(), microphone=()"
    response.headers["Content-Security-Policy"]   = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://pagead2.googlesyndication.com; "
        "style-src 'self' 'unsafe-inline';"
    )
    # Remove server fingerprint
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
# FILE VALIDATION  (magic bytes — not trusting extension alone)
# ─────────────────────────────────────────────────────────────────
def _detect_mime(file_obj) -> str | None:
    """Read first 8 bytes to determine actual MIME type."""
    header = file_obj.read(8)
    file_obj.seek(0)
    for magic, mime in Config.MAGIC_BYTES.items():
        if header.startswith(magic):
            return mime
    return None

def validate_file(file, allowed_ext: set, max_bytes: int = Config.MAX_FILE_SIZE) -> str | None:
    """
    Full file validation.
    Returns error string or None if valid.
    """
    if not file or file.filename == "":
        return "No file provided"

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if ext not in allowed_ext:
        return f"Extension '{ext}' not allowed. Expected: {allowed_ext}"

    mime = _detect_mime(file)
    if mime is None:
        return "File type not recognised (magic bytes invalid)"

    # Cross-check extension vs magic bytes
    if ext == "pdf" and mime != "application/pdf":
        return "File claims to be PDF but is not"
    if ext in {"jpg", "jpeg"} and mime != "image/jpeg":
        return "File claims to be JPEG but is not"
    if ext == "png" and mime != "image/png":
        return "File claims to be PNG but is not"

    # Size check (seek to end)
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)
    if size > max_bytes:
        return f"File too large ({size // 1024 // 1024}MB). Max {max_bytes // 1024 // 1024}MB"
    if size == 0:
        return "File is empty"

    return None


# ─────────────────────────────────────────────────────────────────
# CONTEXT-MANAGER TEMP FILE LIFECYCLE
# ─────────────────────────────────────────────────────────────────
@contextmanager
def temp_upload(file):
    """Save upload, yield path, always clean up."""
    ext  = file.filename.rsplit(".", 1)[-1].lower()
    name = f"{uuid.uuid4()}.{ext}"
    path = os.path.join(Config.UPLOAD_FOLDER, name)
    try:
        file.save(path)
        yield path
    finally:
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                log.warning(f"Could not remove temp upload {path}: {e}")

@contextmanager
def temp_uploads(files):
    """Multi-file variant."""
    paths = []
    try:
        for f in files:
            ext  = f.filename.rsplit(".", 1)[-1].lower()
            name = f"{uuid.uuid4()}.{ext}"
            path = os.path.join(Config.UPLOAD_FOLDER, name)
            f.save(path)
            paths.append(path)
        yield paths
    finally:
        for p in paths:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass

def output_path(suffix: str) -> str:
    """Generate a unique output file path."""
    return os.path.join(Config.OUTPUT_FOLDER, f"{uuid.uuid4()}_{secure_filename(suffix)}")

def output_url(path: str) -> str:
    return f"/download/{os.path.basename(path)}"


# ─────────────────────────────────────────────────────────────────
# RESPONSE HELPERS
# ─────────────────────────────────────────────────────────────────
def err(msg: str, code: int = 400):
    log.warning({"error": msg, "code": code})
    return jsonify({"success": False, "message": msg}), code

def ok(msg: str, path: str = None, **extras):
    payload = {"success": True, "message": msg, **extras}
    if path:
        size = os.path.getsize(path)
        payload["filename"]    = os.path.basename(path)
        payload["download_url"] = output_url(path)
        payload["size_bytes"]   = size
        payload["size_human"]   = f"{size / 1024:.1f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.2f} MB"
        payload["expires_in"]   = f"{Config.FILE_TTL_SEC // 60} minutes"
    return jsonify(payload)


# ─────────────────────────────────────────────────────────────────
# BACKGROUND FILE REAPER (cleans outputs older than FILE_TTL_SEC)
# ─────────────────────────────────────────────────────────────────
def _reap_old_files():
    while True:
        try:
            cutoff = time.time() - Config.FILE_TTL_SEC
            for fname in os.listdir(Config.OUTPUT_FOLDER):
                fpath = os.path.join(Config.OUTPUT_FOLDER, fname)
                if os.path.isfile(fpath) and os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
                    log.info(f"Reaped expired output: {fname}")
        except Exception as e:
            log.error(f"Reaper error: {e}")
        time.sleep(300)  # run every 5 minutes

threading.Thread(target=_reap_old_files, daemon=True, name="file-reaper").start()


# ─────────────────────────────────────────────────────────────────
# STATIC + DOWNLOAD ROUTES
# ─────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return send_from_directory("static", "index.html")

@app.route("/download/<filename>")
def download(filename):
    safe = secure_filename(filename)
    # Prevent path traversal: ensure it's ONLY a filename, no slashes
    if safe != filename or "/" in filename or "\\" in filename:
        return err("Invalid filename", 400)
    path = os.path.join(Config.OUTPUT_FOLDER, safe)
    if not os.path.exists(path):
        return err("File not found or expired", 404)
    # Stream large files rather than loading into RAM
    return send_file(path, as_attachment=True, conditional=True)


# ─────────────────────────────────────────────────────────────────
# ① MERGE PDF
# ─────────────────────────────────────────────────────────────────
@app.route("/api/merge", methods=["POST"])
@rate_limited()
def merge_pdf():
    files = request.files.getlist("files")
    if len(files) < 2:
        return err("At least 2 PDF files required")
    if len(files) > Config.MAX_FILES_MERGE:
        return err(f"Max {Config.MAX_FILES_MERGE} files allowed per merge")

    for f in files:
        e = validate_file(f, Config.ALLOWED_PDF_EXT)
        if e:
            return err(e)

    try:
        with temp_uploads(files) as paths:
            merger = PdfMerger()
            for p in paths:
                merger.append(p)
            out = output_path("merged.pdf")
            merger.write(out)
            merger.close()
        return ok("Merged successfully", path=out)
    except Exception as e:
        log.exception("merge_pdf failed")
        return err("Merge failed", 500)


# ─────────────────────────────────────────────────────────────────
# ② SPLIT PDF
# ─────────────────────────────────────────────────────────────────
@app.route("/api/split", methods=["POST"])
@rate_limited()
def split_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF_EXT)
    if e:
        return err(e)

    try:
        with temp_upload(f) as path:
            reader = PdfReader(path)
            if len(reader.pages) > 500:
                return err("PDF has too many pages (max 500)")

            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for i, page in enumerate(reader.pages):
                    writer = PdfWriter()
                    writer.add_page(page)
                    buf = io.BytesIO()
                    writer.write(buf)
                    zf.writestr(f"page_{i+1:04d}.pdf", buf.getvalue())

            out = output_path("split_pages.zip")
            with open(out, "wb") as fh:
                fh.write(zip_buf.getvalue())

        return ok(f"Split into {len(reader.pages)} pages", path=out)
    except Exception:
        log.exception("split_pdf failed")
        return err("Split failed", 500)


# ─────────────────────────────────────────────────────────────────
# ③ COMPRESS PDF
# ─────────────────────────────────────────────────────────────────
@app.route("/api/compress", methods=["POST"])
@rate_limited()
def compress_pdf():
    f    = request.files.get("file")
    e    = validate_file(f, Config.ALLOWED_PDF_EXT)
    if e:
        return err(e)

    # Quality: low / medium / high  (default: medium)
    quality = request.form.get("quality", "medium").lower()
    dpi_map = {"low": 72, "medium": 120, "high": 150}
    dpi = dpi_map.get(quality, 120)

    try:
        with temp_upload(f) as path:
            orig_size = os.path.getsize(path)
            doc = fitz.open(path)
            out = output_path("compressed.pdf")

            # Compress images inside pages
            for page in doc:
                for img in page.get_images(full=True):
                    xref = img[0]
                    try:
                        base = doc.extract_image(xref)
                        pil  = Image.open(io.BytesIO(base["image"])).convert("RGB")
                        buf  = io.BytesIO()
                        pil.save(buf, format="JPEG", quality=int(dpi * 0.6), optimize=True)
                        doc.update_stream(xref, buf.getvalue())
                    except Exception:
                        pass  # skip unprocessable images

            doc.save(out, deflate=True, garbage=4, clean=True)
            doc.close()

            new_size  = os.path.getsize(out)
            reduction = round((1 - new_size / orig_size) * 100, 1) if orig_size else 0

        return ok("Compressed successfully", path=out,
                  original_size=orig_size, reduction_pct=reduction)
    except Exception:
        log.exception("compress_pdf failed")
        return err("Compression failed", 500)


# ─────────────────────────────────────────────────────────────────
# ④ IMAGE → PDF
# ─────────────────────────────────────────────────────────────────
@app.route("/api/image-to-pdf", methods=["POST"])
@rate_limited()
def image_to_pdf():
    files = request.files.getlist("files")
    if not files:
        return err("No image files provided")
    if len(files) > 50:
        return err("Max 50 images per conversion")

    for f in files:
        e = validate_file(f, Config.ALLOWED_IMAGE_EXT, max_bytes=20 * 1024 * 1024)
        if e:
            return err(e)

    try:
        with temp_uploads(files) as paths:
            images = []
            for p in paths:
                try:
                    img = Image.open(p).convert("RGB")
                    images.append(img)
                except UnidentifiedImageError:
                    return err(f"Could not open image: {os.path.basename(p)}")

            out = output_path("images_to_pdf.pdf")
            images[0].save(out, save_all=True, append_images=images[1:],
                           resolution=150, optimize=True)
        return ok(f"Converted {len(images)} image(s) to PDF", path=out)
    except Exception:
        log.exception("image_to_pdf failed")
        return err("Conversion failed", 500)


# ─────────────────────────────────────────────────────────────────
# ⑤ PDF → IMAGES
# ─────────────────────────────────────────────────────────────────
@app.route("/api/pdf-to-image", methods=["POST"])
@rate_limited()
def pdf_to_image():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF_EXT)
    if e:
        return err(e)

    fmt = request.form.get("format", "png").lower()
    if fmt not in {"png", "jpg"}:
        return err("Format must be png or jpg")
    dpi = min(int(request.form.get("dpi", 150)), 300)

    try:
        with temp_upload(f) as path:
            doc = fitz.open(path)
            if len(doc) > 100:
                return err("PDF too large for image export (max 100 pages)")

            zip_buf = io.BytesIO()
            with zipfile.ZipFile(zip_buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                for i, page in enumerate(doc):
                    mat  = fitz.Matrix(dpi / 72, dpi / 72)
                    pix  = page.get_pixmap(matrix=mat, alpha=False)
                    data = pix.tobytes(fmt)
                    zf.writestr(f"page_{i+1:04d}.{fmt}", data)
            doc.close()

        out = output_path(f"pdf_pages.zip")
        with open(out, "wb") as fh:
            fh.write(zip_buf.getvalue())
        return ok(f"Exported {len(doc)} page(s) as {fmt.upper()}", path=out)
    except Exception:
        log.exception("pdf_to_image failed")
        return err("Export failed", 500)


# ─────────────────────────────────────────────────────────────────
# ⑥ ROTATE PDF
# ─────────────────────────────────────────────────────────────────
@app.route("/api/rotate", methods=["POST"])
@rate_limited()
def rotate_pdf():
    f   = request.files.get("file")
    e   = validate_file(f, Config.ALLOWED_PDF_EXT)
    if e:
        return err(e)

    try:
        angle = int(request.form.get("angle", 90))
    except ValueError:
        return err("Angle must be an integer")
    if angle not in {90, 180, 270}:
        return err("Angle must be 90, 180, or 270")

    try:
        with temp_upload(f) as path:
            reader = PdfReader(path)
            writer = PdfWriter()
            for page in reader.pages:
                page.rotate(angle)
                writer.add_page(page)
            out = output_path("rotated.pdf")
            with open(out, "wb") as fh:
                writer.write(fh)
        return ok(f"Rotated {angle}°", path=out)
    except Exception:
        log.exception("rotate_pdf failed")
        return err("Rotate failed", 500)


# ─────────────────────────────────────────────────────────────────
# ⑦ ADD WATERMARK
# ─────────────────────────────────────────────────────────────────
@app.route("/api/watermark", methods=["POST"])
@rate_limited()
def watermark_pdf():
    f    = request.files.get("file")
    e    = validate_file(f, Config.ALLOWED_PDF_EXT)
    if e:
        return err(e)

    text    = request.form.get("text", "PDFWala").strip()[:80]  # cap length
    opacity = min(max(float(request.form.get("opacity", 0.3)), 0.05), 1.0)
    color   = request.form.get("color", "gray").lower()
    color_map = {"gray": (0.5, 0.5, 0.5), "red": (0.8, 0.1, 0.1),
                 "blue": (0.1, 0.1, 0.8), "black": (0, 0, 0)}
    rgb = color_map.get(color, (0.5, 0.5, 0.5))

    def _make_wm_page(width, height):
        buf = io.BytesIO()
        c   = canvas.Canvas(buf, pagesize=(width, height))
        c.setFillColorRGB(*rgb, alpha=opacity)
        c.setFont("Helvetica-Bold", min(width / len(text) * 1.4, 60))
        c.saveState()
        c.translate(width / 2, height / 2)
        c.rotate(45)
        c.drawCentredString(0, 0, text)
        c.restoreState()
        c.save()
        buf.seek(0)
        return PdfReader(buf).pages[0]

    try:
        with temp_upload(f) as path:
            reader = PdfReader(path)
            writer = PdfWriter()
            for page in reader.pages:
                w = float(page.mediabox.width)
                h = float(page.mediabox.height)
                wm = _make_wm_page(w, h)
                page.merge_page(wm)
                writer.add_page(page)
            out = output_path("watermarked.pdf")
            with open(out, "wb") as fh:
                writer.write(fh)
        return ok("Watermark applied", path=out)
    except Exception:
        log.exception("watermark_pdf failed")
        return err("Watermark failed", 500)


# ─────────────────────────────────────────────────────────────────
# ⑧ PROTECT PDF (add password)
# ─────────────────────────────────────────────────────────────────
@app.route("/api/protect", methods=["POST"])
@rate_limited()
def protect_pdf():
    f   = request.files.get("file")
    e   = validate_file(f, Config.ALLOWED_PDF_EXT)
    if e:
        return err(e)

    password = request.form.get("password", "").strip()
    if not password or len(password) < 4:
        return err("Password must be at least 4 characters")
    if len(password) > 128:
        return err("Password too long (max 128 chars)")

    try:
        with temp_upload(f) as path:
            reader = PdfReader(path)
            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            writer.encrypt(password)
            out = output_path("protected.pdf")
            with open(out, "wb") as fh:
                writer.write(fh)
        return ok("Password protection applied", path=out)
    except Exception:
        log.exception("protect_pdf failed")
        return err("Protection failed", 500)


# ─────────────────────────────────────────────────────────────────
# ⑨ UNLOCK PDF (remove password)
# ─────────────────────────────────────────────────────────────────
@app.route("/api/unlock", methods=["POST"])
@rate_limited()
def unlock_pdf():
    f   = request.files.get("file")
    e   = validate_file(f, Config.ALLOWED_PDF_EXT)
    if e:
        return err(e)

    password = request.form.get("password", "").strip()

    try:
        with temp_upload(f) as path:
            reader = PdfReader(path)
            if reader.is_encrypted:
                if not reader.decrypt(password):
                    return err("Incorrect password", 403)
            writer = PdfWriter()
            for page in reader.pages:
                writer.add_page(page)
            out = output_path("unlocked.pdf")
            with open(out, "wb") as fh:
                writer.write(fh)
        return ok("PDF unlocked", path=out)
    except Exception:
        log.exception("unlock_pdf failed")
        return err("Unlock failed", 500)


# ─────────────────────────────────────────────────────────────────
# ⑩ ADD PAGE NUMBERS
# ─────────────────────────────────────────────────────────────────
@app.route("/api/page-numbers", methods=["POST"])
@rate_limited()
def add_page_numbers():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF_EXT)
    if e:
        return err(e)

    position  = request.form.get("position", "bottom-center").lower()
    start_num = max(1, int(request.form.get("start", 1)))
    font_size = min(max(int(request.form.get("font_size", 10)), 6), 24)

    POSITIONS = {
        "bottom-center": lambda w, h: (w / 2, 20),
        "bottom-right":  lambda w, h: (w - 40, 20),
        "bottom-left":   lambda w, h: (40, 20),
        "top-center":    lambda w, h: (w / 2, h - 20),
        "top-right":     lambda w, h: (w - 40, h - 20),
    }
    pos_fn = POSITIONS.get(position, POSITIONS["bottom-center"])

    try:
        with temp_upload(f) as path:
            reader = PdfReader(path)
            writer = PdfWriter()
            for i, page in enumerate(reader.pages):
                w = float(page.mediabox.width)
                h = float(page.mediabox.height)
                buf = io.BytesIO()
                c   = canvas.Canvas(buf, pagesize=(w, h))
                c.setFont("Helvetica", font_size)
                c.setFillColorRGB(0.3, 0.3, 0.3)
                x, y = pos_fn(w, h)
                c.drawCentredString(x, y, str(start_num + i))
                c.save()
                buf.seek(0)
                overlay = PdfReader(buf).pages[0]
                page.merge_page(overlay)
                writer.add_page(page)
            out = output_path("numbered.pdf")
            with open(out, "wb") as fh:
                writer.write(fh)
        return ok("Page numbers added", path=out)
    except Exception:
        log.exception("add_page_numbers failed")
        return err("Failed to add page numbers", 500)


# ─────────────────────────────────────────────────────────────────
# ⑪ REORDER / ORGANIZE PAGES
# ─────────────────────────────────────────────────────────────────
@app.route("/api/organize", methods=["POST"])
@rate_limited()
def organize_pages():
    """
    Body: file (PDF) + order (comma-separated 1-based page numbers)
    e.g.  order=3,1,2  → reorder 3 pages into page3, page1, page2
    """
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF_EXT)
    if e:
        return err(e)

    raw_order = request.form.get("order", "")
    try:
        order = [int(x.strip()) - 1 for x in raw_order.split(",") if x.strip()]
    except ValueError:
        return err("'order' must be comma-separated page numbers, e.g. 3,1,2")

    try:
        with temp_upload(f) as path:
            reader = PdfReader(path)
            n = len(reader.pages)
            if not order:
                order = list(range(n))
            invalid = [i + 1 for i in order if i < 0 or i >= n]
            if invalid:
                return err(f"Page numbers out of range: {invalid}. PDF has {n} pages")
            writer = PdfWriter()
            for idx in order:
                writer.add_page(reader.pages[idx])
            out = output_path("organized.pdf")
            with open(out, "wb") as fh:
                writer.write(fh)
        return ok(f"Pages reordered: {[i+1 for i in order]}", path=out)
    except Exception:
        log.exception("organize_pages failed")
        return err("Organize failed", 500)


# ─────────────────────────────────────────────────────────────────
# ⑫ CROP PDF
# ─────────────────────────────────────────────────────────────────
@app.route("/api/crop", methods=["POST"])
@rate_limited()
def crop_pdf():
    """
    Crop margins: left, right, top, bottom  (% of page dimension, default 0)
    """
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF_EXT)
    if e:
        return err(e)

    try:
        left   = min(max(float(request.form.get("left",   0)), 0), 45)
        right  = min(max(float(request.form.get("right",  0)), 0), 45)
        top    = min(max(float(request.form.get("top",    0)), 0), 45)
        bottom = min(max(float(request.form.get("bottom", 0)), 0), 45)
    except ValueError:
        return err("Crop values must be numbers (0–45%)")

    try:
        with temp_upload(f) as path:
            reader = PdfReader(path)
            writer = PdfWriter()
            for page in reader.pages:
                mb = page.mediabox
                w  = float(mb.width)
                h  = float(mb.height)
                page.mediabox.lower_left  = (w * left  / 100, h * bottom / 100)
                page.mediabox.upper_right = (w - w * right / 100, h - h * top / 100)
                writer.add_page(page)
            out = output_path("cropped.pdf")
            with open(out, "wb") as fh:
                writer.write(fh)
        return ok("PDF cropped", path=out)
    except Exception:
        log.exception("crop_pdf failed")
        return err("Crop failed", 500)


# ─────────────────────────────────────────────────────────────────
# ⑬ PDF INFO
# ─────────────────────────────────────────────────────────────────
@app.route("/api/info", methods=["POST"])
@rate_limited(per_minute=60)
def pdf_info():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF_EXT)
    if e:
        return err(e)

    try:
        with temp_upload(f) as path:
            reader = PdfReader(path)
            meta   = reader.metadata or {}
            doc    = fitz.open(path)
            size   = os.path.getsize(path)

            # Page dimensions
            page_sizes = []
            for i, page in enumerate(doc):
                r = page.rect
                page_sizes.append({
                    "page":   i + 1,
                    "width":  round(r.width, 2),
                    "height": round(r.height, 2),
                })
            doc.close()

            # SHA-256 fingerprint
            sha = hashlib.sha256()
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    sha.update(chunk)

        info = {
            "pages":       len(reader.pages),
            "encrypted":   reader.is_encrypted,
            "size_bytes":  size,
            "size_human":  f"{size / 1024:.1f} KB" if size < 1024*1024 else f"{size/1024/1024:.2f} MB",
            "sha256":      sha.hexdigest(),
            "title":       meta.get("/Title", ""),
            "author":      meta.get("/Author", ""),
            "subject":     meta.get("/Subject", ""),
            "creator":     meta.get("/Creator", ""),
            "producer":    meta.get("/Producer", ""),
            "created":     str(meta.get("/CreationDate", "")),
            "modified":    str(meta.get("/ModDate", "")),
            "page_sizes":  page_sizes[:10],  # first 10 pages only
        }
        return ok("PDF info extracted", **info)
    except Exception:
        log.exception("pdf_info failed")
        return err("Could not read PDF info", 500)


# ─────────────────────────────────────────────────────────────────
# ⑭ HEALTH CHECK (extended)
# ─────────────────────────────────────────────────────────────────
@app.route("/api/health")
def health():
    upload_ok = os.access(Config.UPLOAD_FOLDER, os.W_OK)
    output_ok = os.access(Config.OUTPUT_FOLDER, os.W_OK)
    return jsonify({
        "status":  "ok" if upload_ok and output_ok else "degraded",
        "app":     "PDFWala",
        "version": "2.0.0",
        "storage": {
            "uploads_writable": upload_ok,
            "outputs_writable": output_ok,
            "output_files":     len(os.listdir(Config.OUTPUT_FOLDER)),
        },
        "ts": datetime.utcnow().isoformat() + "Z",
    })


# ─────────────────────────────────────────────────────────────────
# ERROR HANDLERS
# ─────────────────────────────────────────────────────────────────
@app.errorhandler(413)
def file_too_large(e):
    return err(f"File too large. Max {Config.MAX_FILE_SIZE // 1024 // 1024}MB", 413)

@app.errorhandler(404)
def not_found(e):
    return err("Not found", 404)

@app.errorhandler(405)
def method_not_allowed(e):
    return err("Method not allowed", 405)

@app.errorhandler(500)
def server_error(e):
    log.exception("Unhandled 500")
    return err("Internal server error", 500)


# ─────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("🚀  PDFWala v2.0 running on http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
