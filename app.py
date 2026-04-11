#!/usr/bin/env python3
"""
PDFWala - Complete Production Backend V7.0
Upgrades: AES-256 PDF protection, OCR multi-language + skip-existing,
Word/Excel/PPT direct library conversions (no LibreOffice dependency),
real Word compression (ZIP+Pillow), image-to-Excel OCR table detection,
PDF linearization, regex redaction with presets (SSN/email/Aadhaar/PAN),
image signature upload for sign-PDF, multi-position watermarks,
text diff in PDF compare, rich PDF info (fonts/forms/images),
multi-sheet CSV export, image-to-Word OCR mode.
"""

import os
import io
import uuid
import zipfile
import logging
import time
import threading
import subprocess
import tempfile
import shutil
import re
import csv
import json
import struct
import zlib
import difflib
from contextlib import contextmanager
from functools import wraps
from datetime import datetime, date
from typing import Optional, List, Set, Any
from pathlib import Path
from decimal import Decimal

from flask import Flask, request, jsonify, send_file, send_from_directory, g
from werkzeug.utils import secure_filename

import fitz  # PyMuPDF
from PIL import Image, ImageChops, ImageFilter
from PyPDF2 import PdfReader, PdfWriter, PdfMerger
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.pagesizes import letter, A4

# Optional imports with fallbacks
try:
    from docx import Document as DocxDocument
    from docx.shared import Inches, Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

try:
    from openpyxl import load_workbook, Workbook
    from openpyxl.drawing.image import Image as XlImage
    from openpyxl.styles import PatternFill, Font, Alignment, Border
    from openpyxl.utils import get_column_letter
    OPENPYXL_AVAILABLE = True
except ImportError:
    OPENPYXL_AVAILABLE = False

try:
    import msoffcrypto
    MSOFFCRYPTO_AVAILABLE = True
except ImportError:
    MSOFFCRYPTO_AVAILABLE = False

try:
    from pdf2docx import Converter as Pdf2DocxConverter
    PDF2DOCX_AVAILABLE = True
except ImportError:
    PDF2DOCX_AVAILABLE = False

try:
    import tabula
    TABULA_AVAILABLE = True
except ImportError:
    TABULA_AVAILABLE = False

try:
    import pytesseract
    from pytesseract import Output as TesseractOutput
    TESSERACT_AVAILABLE = True
except ImportError:
    TESSERACT_AVAILABLE = False

try:
    from pptx import Presentation
    from pptx.util import Inches as PptxInches, Pt as PptxPt
    PPTX_AVAILABLE = True
except ImportError:
    PPTX_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────
class Config:
    BASE_DIR = os.environ.get("BASE_DIR", "/app")
    UPLOAD_FOLDER = os.environ.get("UPLOAD_FOLDER", os.path.join(BASE_DIR, "uploads"))
    OUTPUT_FOLDER = os.environ.get("OUTPUT_FOLDER", os.path.join(BASE_DIR, "outputs"))
    STATIC_FOLDER = os.environ.get("STATIC_FOLDER", os.path.join(BASE_DIR, "static"))
    MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE", 200 * 1024 * 1024))
    MAX_FILES_MERGE = int(os.environ.get("MAX_FILES_MERGE", 30))
    FILE_TTL_SEC = int(os.environ.get("FILE_TTL_SEC", 3600))
    RATE_LIMIT = int(os.environ.get("RATE_LIMIT", 30))
    SECRET_KEY = os.environ.get("SECRET_KEY", uuid.uuid4().hex)
    LIBREOFFICE = os.environ.get("LIBREOFFICE_PATH", "soffice")
    GHOSTSCRIPT = os.environ.get("GHOSTSCRIPT_PATH", "gs")

    ALLOWED_PDF = {"pdf"}
    ALLOWED_IMAGE = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tiff"}
    ALLOWED_DOC = {"doc", "docx"}
    ALLOWED_XLS = {"xls", "xlsx"}
    ALLOWED_HTML = {"html", "htm"}
    ALLOWED_WEBP = {"webp"}
    ALLOWED_PNG = {"png"}
    ALLOWED_JPG = {"jpg", "jpeg"}

    OLE_MAGIC = b"\xd0\xcf\x11\xe0"

    # Conversion job timeout (seconds) for pdf-to-word polling
    PDF2WORD_TIMEOUT = int(os.environ.get("PDF2WORD_TIMEOUT", 300))


for _dir in [Config.UPLOAD_FOLDER, Config.OUTPUT_FOLDER]:
    os.makedirs(_dir, exist_ok=True)

_APP_START = time.time()


# ─────────────────────────────────────────────────────────────────
# APP INITIALIZATION
# ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = Config.MAX_FILE_SIZE
app.secret_key = Config.SECRET_KEY

# Celery Setup for Async Tasks
from celery import Celery

def make_celery(app):
    celery = Celery(
        app.import_name,
        broker=os.environ.get('REDIS_URL', 'redis://redis:6379/0'),
        backend=os.environ.get('REDIS_URL', 'redis://redis:6379/0')
    )
    celery.conf.update(
        task_track_started=True,
        task_time_limit=600,
        task_soft_time_limit=540,
        worker_max_tasks_per_child=50,
    )
    return celery

celery = make_celery(app)

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger("pdfwala")

# In-memory job store for pdf-to-word async tracking
_job_store: dict = {}
_job_store_lock = threading.Lock()
    # Helper for progress tracking
def get_pdf_page_count(pdf_path: str) -> int:
    """Quick page count without full document load."""
    try:
        doc = fitz.open(pdf_path)
        count = len(doc)
        doc.close()
        return count
    except:
        return 1


@app.before_request
def _before():
    g.start = time.time()
    g.request_id = str(uuid.uuid4())[:8]


@app.after_request
def _after(response):
    ms = round((time.time() - g.get("start", time.time())) * 1000, 1)
    log.info(f"{request.method} {request.path} → {response.status_code} [{ms}ms] [{g.get('request_id','-')}]")
    response.headers["X-Request-ID"] = g.get("request_id", "-")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers.pop("Server", None)
    return response


# ─────────────────────────────────────────────────────────────────
# RATE LIMITER
# ─────────────────────────────────────────────────────────────────
_rate_store: dict = {}
_rate_lock = threading.Lock()


def rate_limited():
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            xff = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown")
            ip = xff.split(",")[0].strip()
            now = time.monotonic()
            with _rate_lock:
                hits = [t for t in _rate_store.get(ip, []) if now - t < 60.0]
                if len(hits) >= Config.RATE_LIMIT:
                    return jsonify({"success": False, "error": "Rate limit exceeded. Try again in 60 seconds."}), 429
                hits.append(now)
                _rate_store[ip] = hits
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# ─────────────────────────────────────────────────────────────────
# FILE VALIDATION
# ─────────────────────────────────────────────────────────────────
def _detect_mime(file_obj) -> Optional[str]:
    header = file_obj.read(512)
    file_obj.seek(0)
    if header[:4] == Config.OLE_MAGIC:
        return "application/msoffice"
    if header[:4] == b"PK\x03\x04":
        chunk = file_obj.read(2048)
        file_obj.seek(0)
        if b"word/" in chunk:
            return "application/msword"
        if b"xl/" in chunk:
            return "application/vnd.ms-excel"
        if b"ppt/" in chunk:
            return "application/vnd.ms-powerpoint"
        return "application/zip"
    if header[:4] == b"%PDF":
        return "application/pdf"
    if header[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if header[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if header[:2] == b"BM":
        return "image/bmp"
    if header[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if b"<!DOCTYPE" in header or b"<html" in header.lower():
        return "text/html"
    return None


def validate_file(file, allowed_ext: Set[str]) -> Optional[str]:
    if not file or not file.filename:
        return "No file provided"

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""

    if ext not in allowed_ext:
        return f"Invalid file type. Allowed: {', '.join(sorted(allowed_ext))}"

    file.seek(0, 2)
    size = file.tell()
    file.seek(0)

    if size == 0:
        return "File is empty"

    if size > Config.MAX_FILE_SIZE:
        return f"File too large (max {Config.MAX_FILE_SIZE // 1048576} MB)"

    if ext in {"html", "htm"}:
        return None

    mime = _detect_mime(file)
    if mime in ("application/msoffice", "application/msword",
                "application/vnd.ms-excel", "application/vnd.ms-powerpoint",
                "application/zip"):
        return None

    mime_ext_map = {
        "application/pdf": {"pdf"},
        "image/jpeg": {"jpg", "jpeg"},
        "image/png": {"png"},
        "image/webp": {"webp"},
        "image/gif": {"gif"},
        "image/bmp": {"bmp"},
        "image/tiff": {"tiff"},
        "text/html": {"html", "htm"},
    }

    if mime and ext not in mime_ext_map.get(mime, {ext}):
        return f"File content does not match extension .{ext}"

    return None


# ─────────────────────────────────────────────────────────────────
# UTILITY FUNCTIONS
# ─────────────────────────────────────────────────────────────────
def generate_output_filename(original_filename: str, operation: str,
                              is_multi: bool = False, filenames: list = None) -> str:
    if is_multi and filenames and len(filenames) > 1:
        stems = [Path(f).stem for f in filenames]
        common = os.path.commonprefix(stems).rstrip('_-')
        name = common if len(common) > 2 else "merged_documents"
        ext = '.pdf'
    else:
        name = Path(original_filename).stem
        for suffix in ['_compressed', '_merged', '_rotated', '_watermarked',
                       '_protected', '_unlocked', '_cropped', '_converted',
                       '_to_jpg', '_to_png', '_to_txt', '_to_excel', '_to_ppt',
                       '_to_html', '_to_json', '_edited']:
            if name.endswith(suffix):
                name = name[:-len(suffix)]
                break
        ext = Path(original_filename).suffix

    name = re.sub(r'[^\w\-_.]', '_', name)
    final_name = f"{name}_{operation}{ext}"

    if operation in ['split_pages', 'to_jpg', 'to_png', 'comparison', 'to_image']:
        final_name = re.sub(r'\.\w+$', '.zip', final_name)
        if not final_name.endswith('.zip'):
            final_name = Path(final_name).stem + '.zip'

    return final_name


@contextmanager
def temp_upload(file):
    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else "bin"
    path = os.path.join(Config.UPLOAD_FOLDER, f"{uuid.uuid4()}.{ext}")
    try:
        file.save(path)
        yield path
    finally:
        try:
            os.remove(path)
        except Exception:
            pass


@contextmanager
def temp_uploads(files):
    paths = []
    try:
        for f in files:
            ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else "bin"
            path = os.path.join(Config.UPLOAD_FOLDER, f"{uuid.uuid4()}.{ext}")
            f.save(path)
            paths.append(path)
        yield paths
    finally:
        for p in paths:
            try:
                os.remove(p)
            except Exception:
                pass


def err(msg: str, code: int = 400):
    log.warning(f"[{g.get('request_id', '-')}] ERR {code}: {msg}")
    return jsonify({"success": False, "error": msg}), code


def ok(msg: str, path: str = None, **extras):
    payload = {"success": True, "message": msg, **extras}
    if path and os.path.exists(path):
        fname = os.path.basename(path)
        size = os.path.getsize(path)
        payload.update({
            "download_url": f"/download/{fname}",
            "filename": fname,
            "size_human": f"{size/1048576:.2f} MB" if size > 1048576 else f"{size/1024:.1f} KB",
            "expires_in": f"{Config.FILE_TTL_SEC // 60} minutes"
        })
    return jsonify(payload)


def sanitize(text: str, maxlen: int = 500) -> str:
    return (text or "").strip()[:maxlen]


# ─────────────────────────────────────────────────────────────────
# LIBREOFFICE HELPER
# ─────────────────────────────────────────────────────────────────
def libre(input_path: str, fmt: str, output_filename: str = None, temp: bool = False) -> Optional[str]:
    """
    Run LibreOffice headless conversion.
    Validates that output file actually exists AND has non-zero size before returning.
    Returns None on any failure so callers can fall back cleanly.
    """
    out_dir = tempfile.mkdtemp()
    try:
        result = subprocess.run(
            [Config.LIBREOFFICE, "--headless", "--convert-to", fmt, "--outdir", out_dir, input_path],
            capture_output=True,
            timeout=6000
        )
        if result.returncode != 0:
            log.error(f"LibreOffice failed (rc={result.returncode}): {result.stderr.decode()[:500]}")
            return None

        base = os.path.splitext(os.path.basename(input_path))[0]
        converted = os.path.join(out_dir, f"{base}.{fmt}")
        if not os.path.exists(converted):
            matches = list(Path(out_dir).glob(f"*.{fmt}"))
            if not matches:
                log.error(f"LibreOffice produced no output for format {fmt}")
                return None
            converted = str(matches[0])

        if os.path.getsize(converted) == 0:
            log.error(f"LibreOffice produced empty output file for format {fmt}")
            return None

        if temp:
            final = os.path.join(tempfile.gettempdir(), f"{uuid.uuid4()}.{fmt}")
        elif output_filename:
            final = os.path.join(Config.OUTPUT_FOLDER, output_filename)
        else:
            final = os.path.join(Config.OUTPUT_FOLDER, f"{uuid.uuid4()}_output.{fmt}")

        shutil.move(converted, final)
        return final

    except subprocess.TimeoutExpired:
        log.error("LibreOffice timed out")
        return None
    except Exception as e:
        log.error(f"LibreOffice exception: {e}")
        return None
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────
# GHOSTSCRIPT HELPER
# ─────────────────────────────────────────────────────────────────
def ghostscript_compress(input_path: str, output_path: str, gs_setting: str = "/ebook",
                          extra_flags: list = None) -> bool:
    cmd = [
        Config.GHOSTSCRIPT,
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.4",
        f"-dPDFSETTINGS={gs_setting}",
        "-dNOPAUSE", "-dBATCH", "-dQUIET",
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true",
        "-dSubsetFonts=true",
        "-dEmbedAllFonts=false",
        "-dFastWebView=true",
        "-dAutoRotatePages=/None",
        f"-sOutputFile={output_path}",
    ]
    if extra_flags:
        cmd.extend(extra_flags)
    cmd.append(input_path)

    try:
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        if result.returncode != 0:
            log.error(f"GS failed: {result.stderr.decode()[:300]}")
            return False
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            log.error("GS produced empty/missing output")
            return False
        return True
    except subprocess.TimeoutExpired:
        log.error("Ghostscript timed out")
        return False
    except Exception as e:
        log.error(f"Ghostscript exception: {e}")
        return False


# ─────────────────────────────────────────────────────────────────
# EXCEL TYPE PRESERVATION HELPERS
# ─────────────────────────────────────────────────────────────────
def _coerce_cell_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e15:
            return int(value)
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        f = float(value)
        if f == int(f):
            return int(f)
        return f
    return str(value)


def _coerce_cell_for_csv(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e15:
            return str(int(value))
        return f"{value:.10g}"
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


# ─────────────────────────────────────────────────────────────────
# WATERMARK HELPER  (V7: position support)
# ─────────────────────────────────────────────────────────────────
def _make_watermark(text: str, opacity: float, color_hex: str,
                    page_width: float, page_height: float,
                    position: str = "diagonal") -> bytes:
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(page_width, page_height))

    try:
        r = int(color_hex[0:2], 16) / 255
        g_val = int(color_hex[2:4], 16) / 255
        b = int(color_hex[4:6], 16) / 255
    except Exception:
        r, g_val, b = 0.5, 0.5, 0.5

    alpha = max(0.05, min(opacity, 0.95))
    c.setFillColorRGB(r, g_val, b, alpha=alpha)
    font_size = min(page_width, page_height) * 0.08
    c.setFont("Helvetica-Bold", font_size)

    if position == "diagonal":
        c.saveState()
        c.translate(page_width / 2, page_height / 2)
        c.rotate(45)
        c.drawCentredString(0, 0, text)
        c.drawCentredString(0, min(page_width, page_height) * 0.12, text)
        c.drawCentredString(0, -min(page_width, page_height) * 0.12, text)
        c.restoreState()
    elif position == "center":
        c.drawCentredString(page_width / 2, page_height / 2, text)
    elif position == "top":
        c.drawCentredString(page_width / 2, page_height * 0.95 - font_size, text)
    elif position == "bottom":
        c.drawCentredString(page_width / 2, page_height * 0.05, text)
    elif position == "tile":
        # 3x3 grid
        for row_i in range(3):
            for col_i in range(3):
                x = page_width * (col_i + 0.5) / 3
                y = page_height * (row_i + 0.5) / 3
                c.saveState()
                c.translate(x, y)
                c.rotate(30)
                c.drawCentredString(0, 0, text)
                c.restoreState()
    else:
        # default diagonal
        c.saveState()
        c.translate(page_width / 2, page_height / 2)
        c.rotate(45)
        c.drawCentredString(0, 0, text)
        c.restoreState()

    c.save()
    buf.seek(0)
    return buf.read()


# ─────────────────────────────────────────────────────────────────
# PAGE NUMBER HELPER
# ─────────────────────────────────────────────────────────────────
def _make_page_num(label: str, position: str,
                   page_width: float, page_height: float) -> bytes:
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(page_width, page_height))
    c.setFont("Helvetica", 10)
    c.setFillColorRGB(0.2, 0.2, 0.2)

    y = page_height - 20 if position == "top" else 15
    c.drawCentredString(page_width / 2, y, label)
    c.save()
    buf.seek(0)
    return buf.read()


# ─────────────────────────────────────────────────────────────────
# BACKGROUND CLEANUP WORKER  (V7: also cleans UPLOAD_FOLDER)
# ─────────────────────────────────────────────────────────────────
def _cleanup_worker():
    while True:
        try:
            now = time.time()
            for folder in [Config.OUTPUT_FOLDER, Config.UPLOAD_FOLDER]:
                for fname in os.listdir(folder):
                    fpath = os.path.join(folder, fname)
                    try:
                        if os.path.isfile(fpath):
                            age = now - os.path.getmtime(fpath)
                            if age > Config.FILE_TTL_SEC:
                                os.remove(fpath)
                                log.info(f"Cleaned up expired file: {fname}")
                    except Exception as e:
                        log.warning(f"Cleanup error for {fname}: {e}")

            with _job_store_lock:
                stale = [jid for jid, j in _job_store.items()
                         if now - j.get("created_at", now) > Config.FILE_TTL_SEC]
                for jid in stale:
                    del _job_store[jid]
        except Exception as e:
            log.error(f"Cleanup worker error: {e}")
        time.sleep(600)


_cleanup_thread = threading.Thread(target=_cleanup_worker, daemon=True)
_cleanup_thread.start()


# ─────────────────────────────────────────────────────────────────
# PAGE RANGE PARSER
# ─────────────────────────────────────────────────────────────────
def _parse_pages(spec: str, total: int) -> List[int]:
    indices = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                a, b = int(a.strip()), int(b.strip())
                for i in range(max(1, a), min(b, total) + 1):
                    indices.add(i - 1)
            except ValueError:
                pass
        else:
            try:
                n = int(part)
                if 1 <= n <= total:
                    indices.add(n - 1)
            except ValueError:
                pass
    return sorted(indices)


# ─────────────────────────────────────────────────────────────────
# ROUTES: STATIC + HEALTH + DOWNLOAD
# ─────────────────────────────────────────────────────────────────
@app.route("/api/health")
def health():
    lo = False
    gs = False
    tess_binary = False
    try:
        subprocess.run([Config.LIBREOFFICE, "--version"], capture_output=True, timeout=5)
        lo = True
    except Exception:
        pass
    try:
        subprocess.run([Config.GHOSTSCRIPT, "--version"], capture_output=True, timeout=5)
        gs = True
    except Exception:
        pass
    try:
        subprocess.run(["tesseract", "--version"], capture_output=True, timeout=5)
        tess_binary = True
    except Exception:
        pass
    return jsonify({
        "success": True,
        "status": "ok",
        "version": "7.0.0",
        "uptime_seconds": round(time.time() - _APP_START, 1),
        "tools_available": {
            "libreoffice": lo,
            "tesseract": TESSERACT_AVAILABLE,
            "tesseract_binary": tess_binary,
            "ghostscript": gs,
            "pdf2docx": PDF2DOCX_AVAILABLE,
            "tabula": TABULA_AVAILABLE,
            "python_docx": DOCX_AVAILABLE,
            "openpyxl": OPENPYXL_AVAILABLE,
            "msoffcrypto": MSOFFCRYPTO_AVAILABLE,
            "python_pptx": PPTX_AVAILABLE,
        }
    })


@app.route("/download/<filename>")
def download(filename):
    safe = secure_filename(filename)
    if not safe or safe != filename or "/" in filename or ".." in filename:
        return err("Invalid filename", 400)

    ALLOWED_EXTS = (".pdf", ".zip", ".jpg", ".jpeg", ".png",
                    ".docx", ".xlsx", ".pptx", ".txt", ".json", ".html", ".csv", ".webp")
    if not safe.lower().endswith(ALLOWED_EXTS):
        return err("Invalid file type", 400)

    path = os.path.join(Config.OUTPUT_FOLDER, safe)
    if not os.path.exists(path):
        return err("File not found or expired", 404)

    response = send_file(path, as_attachment=True, conditional=True)
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Cache-Control"] = "no-cache"
    return response


# ═════════════════════════════════════════════════════════════════
# PDF ORGANIZE
# ═════════════════════════════════════════════════════════════════
@app.route("/api/merge", methods=["POST"])
@rate_limited()
def merge_pdf():
    files = request.files.getlist("files")
    if len(files) < 2:
        return err("Minimum 2 PDF files required")
    if len(files) > Config.MAX_FILES_MERGE:
        return err(f"Maximum {Config.MAX_FILES_MERGE} files allowed")
    for f in files:
        e = validate_file(f, Config.ALLOWED_PDF)
        if e:
            return err(e)
    try:
        with temp_uploads(files) as paths:
            merger = PdfMerger()
            for p in paths:
                merger.append(p)
            filename = generate_output_filename(
                files[0].filename, "merged",
                is_multi=True, filenames=[f.filename for f in files]
            )
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            merger.write(out)
            merger.close()
        return ok(f"Merged {len(files)} PDFs successfully", out)
    except Exception:
        log.exception("merge")
        return err("Merge failed", 500)


@app.route("/api/split", methods=["POST"])
@rate_limited()
def split_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    mode = request.form.get("mode", "all")
    ranges = request.form.get("ranges", "")
    try:
        with temp_upload(f) as path:
            reader = PdfReader(path)
            total = len(reader.pages)
            indices = list(range(total)) if mode == "all" else _parse_pages(ranges, total)
            if not indices:
                return err("No valid pages in range")
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for idx in indices:
                    w = PdfWriter()
                    w.add_page(reader.pages[idx])
                    pb = io.BytesIO()
                    w.write(pb)
                    zf.writestr(f"page_{idx+1:04d}.pdf", pb.getvalue())
            operation = "split_pages" if mode == "all" else "extracted_pages"
            filename = generate_output_filename(f.filename, operation)
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(out, "wb") as fh:
                fh.write(buf.getvalue())
        return ok(f"Split into {len(indices)} pages", out)
    except Exception:
        log.exception("split")
        return err("Split failed", 500)


@app.route("/api/organize", methods=["POST"])
@rate_limited()
def organize_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    action = request.form.get("action", "reorder").lower()
    order = request.form.get("order", "").strip()
    if not order:
        return err("Order/pages parameter required")
    try:
        with temp_upload(f) as path:
            reader = PdfReader(path)
            total = len(reader.pages)
            specified = _parse_pages(order, total)
            if not specified:
                return err("No valid pages specified")
            if action == "delete":
                final = [i for i in range(total) if i not in set(specified)]
            elif action == "extract":
                final = specified
            else:
                final = specified
            w = PdfWriter()
            for idx in final:
                w.add_page(reader.pages[idx])
            filename = generate_output_filename(f.filename, "organized")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(out, "wb") as fh:
                w.write(fh)
        labels = {"reorder": "Reordered", "extract": "Extracted", "delete": "Deleted pages from"}
        return ok(f"{labels.get(action, 'Organized')} PDF", out)
    except Exception:
        log.exception("organize")
        return err("Organize failed", 500)


@app.route("/api/remove-pages", methods=["POST"])
@rate_limited()
def remove_pages():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    order = request.form.get("order", "")
    if not order:
        return err("Pages to remove required")
    try:
        with temp_upload(f) as path:
            reader = PdfReader(path)
            total = len(reader.pages)
            remove = set(_parse_pages(order, total))
            w = PdfWriter()
            for i, page in enumerate(reader.pages):
                if i not in remove:
                    w.add_page(page)
            filename = generate_output_filename(f.filename, "pages_removed")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(out, "wb") as fh:
                w.write(fh)
        return ok(f"Removed {len(remove)} page(s)", out)
    except Exception:
        log.exception("remove_pages")
        return err("Remove pages failed", 500)


@app.route("/api/extract-pages", methods=["POST"])
@rate_limited()
def extract_pages():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    order = request.form.get("order", "")
    if not order:
        return err("Pages to extract required")
    try:
        with temp_upload(f) as path:
            reader = PdfReader(path)
            total = len(reader.pages)
            indices = _parse_pages(order, total)
            w = PdfWriter()
            for idx in indices:
                w.add_page(reader.pages[idx])
            filename = generate_output_filename(f.filename, "extracted")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(out, "wb") as fh:
                w.write(fh)
        return ok(f"Extracted {len(indices)} page(s)", out)
    except Exception:
        log.exception("extract_pages")
        return err("Extract pages failed", 500)


# ═════════════════════════════════════════════════════════════════
# PDF OPTIMIZE
# ═════════════════════════════════════════════════════════════════
@app.route("/api/compress", methods=["POST"])
@rate_limited()
def compress_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    quality = request.form.get("quality", "medium").lower()

    cfg = {
        "low":    {"dpi": 150, "quality": 85, "gs": "/printer"},
        "medium": {"dpi": 120, "quality": 72, "gs": "/ebook"},
        "high":   {"dpi": 96,  "quality": 60, "gs": "/screen"},
    }.get(quality, {"dpi": 120, "quality": 72, "gs": "/ebook"})

    try:
        with temp_upload(f) as path:
            orig = os.path.getsize(path)
            stage1 = path + "_stage1.pdf"

            try:
                doc = fitz.open(path)
                try:
                    for page in doc:
                        for img in page.get_images(full=True):
                            xref = img[0]
                            try:
                                base = doc.extract_image(xref)
                                if not base:
                                    continue
                                pil = Image.open(io.BytesIO(base["image"]))
                                orig_w, orig_h = pil.size
                                src_dpi = max(base.get("xres", 150), base.get("yres", 150), 1)
                                scale = min(1.0, cfg["dpi"] / src_dpi)
                                if scale < 0.99:
                                    new_w = max(1, int(orig_w * scale))
                                    new_h = max(1, int(orig_h * scale))
                                    pil = pil.resize((new_w, new_h), Image.LANCZOS)
                                if pil.mode in ("RGBA", "P", "LA"):
                                    bg = Image.new("RGB", pil.size, (255, 255, 255))
                                    if pil.mode == "P":
                                        pil = pil.convert("RGBA")
                                    mask = pil.split()[-1] if pil.mode in ("RGBA", "LA") else None
                                    bg.paste(pil, mask=mask)
                                    pil = bg
                                elif pil.mode != "RGB":
                                    pil = pil.convert("RGB")
                                buf = io.BytesIO()
                                pil.save(buf, format="JPEG", quality=cfg["quality"],
                                         optimize=True, progressive=True)
                                doc.update_stream(xref, buf.getvalue())
                            except Exception:
                                pass
                    doc.save(stage1, deflate=True, deflate_images=True,
                             deflate_fonts=True, garbage=4, clean=True)
                    stage1_size = os.path.getsize(stage1)
                finally:
                    doc.close()
            except Exception as ex:
                log.warning(f"Stage 1 PyMuPDF failed: {ex}. Using original for GS.")
                shutil.copy(path, stage1)
                stage1_size = orig

            filename = generate_output_filename(f.filename, "compressed")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            gs_out = out + "_gs.pdf"

            gs_ok = ghostscript_compress(
                stage1, gs_out, cfg["gs"],
                extra_flags=[
                    "-dColorImageDownsampleType=/Bicubic",
                    "-dGrayImageDownsampleType=/Bicubic",
                    f"-dColorImageResolution={cfg['dpi']}",
                    f"-dGrayImageResolution={cfg['dpi']}",
                    f"-dMonoImageResolution={min(cfg['dpi']*2, 300)}",
                ]
            )

            candidates = []
            if gs_ok and os.path.exists(gs_out):
                candidates.append((os.path.getsize(gs_out), gs_out))
            if os.path.exists(stage1):
                candidates.append((stage1_size, stage1))

            if candidates:
                _, best = min(candidates, key=lambda x: x[0])
                shutil.copy(best, out)
            else:
                shutil.copy(path, out)

            for tmp in [stage1, gs_out]:
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except Exception:
                    pass

            new_size = os.path.getsize(out)
            reduction = round((1 - new_size / orig) * 100, 1) if orig else 0

        return ok(f"Compressed — {reduction}% smaller", out, reduction_pct=reduction)
    except Exception:
        log.exception("compress")
        return err("Compression failed", 500)


@app.route("/api/repair-pdf", methods=["POST"])
@rate_limited()
def repair_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            filename = generate_output_filename(f.filename, "repaired")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)

            gs_ok = ghostscript_compress(
                path, out, "/printer",
                extra_flags=["-dPDFSTOPONERROR=false", "-dPDFSTOPONWARNING=false"]
            )
            if gs_ok and os.path.exists(out) and os.path.getsize(out) > 0:
                return ok("PDF repaired successfully (Ghostscript)", out)

            doc = fitz.open(path)
            try:
                doc.save(out, garbage=4, deflate=True, clean=True)
            finally:
                doc.close()

        return ok("PDF repaired successfully", out)
    except Exception:
        log.exception("repair_pdf")
        return err("Repair failed", 500)


# ═════════════════════════════════════════════════════════════════
# PDF LINEARIZE  [NEW in V7]
# ═════════════════════════════════════════════════════════════════
@app.route("/api/linearize-pdf", methods=["POST"])
@rate_limited()
def linearize_pdf():
    """
    Linearize (web-optimize) a PDF using Ghostscript with -dFastWebView=true.
    Linearized PDFs begin displaying in browsers before fully downloaded.
    """
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            orig_size = os.path.getsize(path)
            filename = generate_output_filename(f.filename, "linearized")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)

            gs_ok = ghostscript_compress(
                path, out, gs_setting="/ebook",
                extra_flags=["-dFastWebView=true"]
            )
            if not gs_ok or not os.path.exists(out) or os.path.getsize(out) == 0:
                return err("Linearization failed — ensure Ghostscript is installed.", 500)

            new_size = os.path.getsize(out)
            reduction = round((1 - new_size / orig_size) * 100, 1) if orig_size else 0

        return ok(
            f"PDF linearized for fast web view — {reduction}% size change",
            out,
            original_size_bytes=orig_size,
            new_size_bytes=new_size,
            reduction_pct=reduction
        )
    except Exception:
        log.exception("linearize_pdf")
        return err("PDF linearization failed", 500)


# ═════════════════════════════════════════════════════════════════
# OCR PDF  (V7: multi-language, psm/oem, skip_existing)
# ═════════════════════════════════════════════════════════════════
@app.route("/api/ocr-pdf", methods=["POST"])
@rate_limited()
def ocr_pdf():
    if not TESSERACT_AVAILABLE:
        return err("OCR requires pytesseract. Install: pip install pytesseract", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)

    # Sanitize lang: allow alphanumeric and +/-
    raw_lang = request.form.get("lang", "eng")
    lang = re.sub(r'[^a-zA-Z0-9+\-]', '', raw_lang)[:50] or "eng"

    # Validate DPI
    try:
        dpi = min(int(request.form.get("dpi", "300")), 400)
    except ValueError:
        return err("dpi must be an integer between 1 and 400")

    # PSM: page segmentation mode 1-13
    try:
        psm = int(request.form.get("psm", "3"))
        if not (1 <= psm <= 13):
            psm = 3
    except ValueError:
        return err("psm must be an integer between 1 and 13")

    # OEM: OCR engine mode 0-3
    try:
        oem = int(request.form.get("oem", "3"))
        if not (0 <= oem <= 3):
            oem = 3
    except ValueError:
        return err("oem must be an integer between 0 and 3")

    skip_existing = request.form.get("skip_existing", "true").lower() in ("true", "1", "yes")

    try:
        with temp_upload(f) as path:
            src_doc = fitz.open(path)
            out_doc = fitz.open()
            pages_processed = 0
            pages_skipped = 0

            try:
                for page_num, src_page in enumerate(src_doc):
                    page_w = src_page.rect.width
                    page_h = src_page.rect.height

                    # Skip pages that already have selectable text
                    if skip_existing and src_page.get_text().strip():
                        new_page = out_doc.new_page(width=page_w, height=page_h)
                        new_page.show_pdf_page(
                            fitz.Rect(0, 0, page_w, page_h),
                            src_doc, page_num, overlay=False
                        )
                        pages_skipped += 1
                        log.info(f"OCR page {page_num+1}: skipped (has text)")
                        continue

                    mat = fitz.Matrix(dpi / 72, dpi / 72)
                    pix = src_page.get_pixmap(matrix=mat, alpha=False, colorspace=fitz.csGRAY)
                    img_data = pix.tobytes("png")
                    img_scale_x = page_w / pix.width
                    img_scale_y = page_h / pix.height

                    try:
                        pil_img = Image.open(io.BytesIO(img_data))
                        hocr_data = pytesseract.image_to_data(
                            pil_img,
                            lang=lang,
                            output_type=TesseractOutput.DICT,
                            config=f"--psm {psm} --oem {oem}"
                        )
                    except Exception as ocr_ex:
                        log.warning(f"OCR failed on page {page_num+1}: {ocr_ex}")
                        hocr_data = None

                    new_page = out_doc.new_page(width=page_w, height=page_h)
                    new_page.show_pdf_page(
                        fitz.Rect(0, 0, page_w, page_h),
                        src_doc, page_num, overlay=False
                    )

                    if hocr_data:
                        n_words = len(hocr_data.get("text", []))
                        for i in range(n_words):
                            word = (hocr_data["text"][i] or "").strip()
                            conf = int(hocr_data["conf"][i]) if hocr_data["conf"][i] != -1 else 0
                            if not word or conf < 30:
                                continue
                            x_px = hocr_data["left"][i]
                            y_px = hocr_data["top"][i]
                            w_px = hocr_data["width"][i]
                            h_px = hocr_data["height"][i]
                            x0 = x_px * img_scale_x
                            y0 = y_px * img_scale_y
                            x1 = (x_px + w_px) * img_scale_x
                            y1 = (y_px + h_px) * img_scale_y
                            if x1 <= x0 or y1 <= y0:
                                continue
                            word_h = y1 - y0
                            fontsize = max(4.0, word_h * 0.85)
                            new_page.insert_text(
                                (x0, y1 - 1),
                                word + " ",
                                fontsize=fontsize,
                                fontname="helv",
                                color=(0, 0, 0),
                                render_mode=3,
                                overlay=True
                            )

                    pages_processed += 1
                    log.info(f"OCR page {page_num+1}/{len(src_doc)} done")

                filename = generate_output_filename(f.filename, "ocr")
                out = os.path.join(Config.OUTPUT_FOLDER, filename)
                out_doc.save(out, deflate=True, garbage=2)
            finally:
                out_doc.close()
                src_doc.close()

        return ok(
            "OCR completed — PDF is now fully text-searchable",
            out,
            output_metadata={
                "pages_processed": pages_processed,
                "pages_skipped": pages_skipped,
                "lang": lang,
                "dpi": dpi,
                "psm": psm,
                "oem": oem
            }
        )
    except Exception:
        log.exception("ocr_pdf")
        return err("OCR failed", 500)


# ═════════════════════════════════════════════════════════════════
# PDF EDIT
# ═════════════════════════════════════════════════════════════════
@app.route("/api/rotate", methods=["POST"])
@rate_limited()
def rotate_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    try:
        angle = int(request.form.get("angle", "90"))
    except ValueError:
        return err("Angle must be an integer (90, 180, or 270)")
    pages = request.form.get("pages", "all").strip()
    if angle not in (90, 180, 270):
        return err("Angle must be 90, 180, or 270")
    try:
        with temp_upload(f) as path:
            reader = PdfReader(path)
            total = len(reader.pages)
            w = PdfWriter()
            idxs = list(range(total)) if pages.lower() == "all" else _parse_pages(pages, total)
            for i, page in enumerate(reader.pages):
                if i in idxs:
                    page.rotate(angle)
                w.add_page(page)
            filename = generate_output_filename(f.filename, "rotated")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(out, "wb") as fh:
                w.write(fh)
        return ok(f"Rotated {len(idxs)} page(s) by {angle}°", out)
    except Exception:
        log.exception("rotate")
        return err("Rotate failed", 500)


@app.route("/api/watermark", methods=["POST"])
@rate_limited()
def watermark_pdf():
    """
    V7.1 UPGRADE: Added rotation angle control.
    - rotation: angle in degrees (default: 45)
    """
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)

    text = sanitize(request.form.get("text", "CONFIDENTIAL"))
    color = sanitize(request.form.get("color", "808080"), 10)
    opacity = float(request.form.get("opacity", "0.3"))
    position = sanitize(request.form.get("position", "diagonal"), 20)
    
    # NEW: Rotation control
    try:
        rotation = float(request.form.get("rotation", "45"))
        rotation = max(-90, min(90, rotation))  # Clamp to reasonable range
    except (ValueError, TypeError):
        rotation = 45.0
    
    if position not in ("diagonal", "center", "top", "bottom", "tile"):
        position = "diagonal"

    try:
        scale = float(request.form.get("scale", "0.3"))
        scale = max(0.1, min(1.0, scale))
    except (ValueError, TypeError):
        scale = 0.3

    image_file = request.files.get("image")
    image_data = None
    if image_file and image_file.filename:
        image_data = image_file.read()

    try:
        with temp_upload(f) as path:
            doc = fitz.open(path)
            try:
                for page in doc:
                    r = page.rect

                    if image_data:
                        # Image watermark
                        img = Image.open(io.BytesIO(image_data)).convert("RGBA")
                        r_ch, g_ch, b_ch, a_ch = img.split()
                        a_ch = a_ch.point(lambda x: int(x * opacity))
                        img.putalpha(a_ch)
                        
                        # Apply rotation to image
                        if rotation != 0:
                            img = img.rotate(rotation, expand=True, resample=Image.BICUBIC)
                        
                        img_buf = io.BytesIO()
                        img.save(img_buf, format="PNG")
                        img_buf.seek(0)

                        img_w = r.width * scale
                        img_h = img_w * img.height / img.width

                        if position == "center":
                            ix = r.x0 + (r.width - img_w) / 2
                            iy = r.y0 + (r.height - img_h) / 2
                        elif position == "top":
                            ix = r.x0 + (r.width - img_w) / 2
                            iy = r.y0 + r.height * 0.05
                        elif position == "bottom":
                            ix = r.x0 + (r.width - img_w) / 2
                            iy = r.y1 - img_h - r.height * 0.05
                        else:
                            ix = r.x0 + (r.width - img_w) / 2
                            iy = r.y0 + (r.height - img_h) / 2

                        img_rect = fitz.Rect(ix, iy, ix + img_w, iy + img_h)
                        page.insert_image(img_rect, stream=img_buf.getvalue(), overlay=True)
                    else:
                        # Text watermark with rotation
                        wm_bytes = _make_watermark_with_rotation(
                            text, opacity, color, r.width, r.height, position, rotation
                        )
                        wmpdf = fitz.open("pdf", wm_bytes)
                        page.show_pdf_page(fitz.Rect(0, 0, r.width, r.height), wmpdf, 0, overlay=True)
                        wmpdf.close()

                filename = generate_output_filename(f.filename, "watermarked")
                out = os.path.join(Config.OUTPUT_FOLDER, filename)
                doc.save(out)
            finally:
                doc.close()
        wm_type = "image" if image_data else "text"
        return ok(f"Watermark ({wm_type}, pos={position}, rot={rotation}°) added", out)
    except Exception:
        log.exception("watermark")
        return err("Watermark failed", 500)


# Add this helper function
def _make_watermark_with_rotation(text: str, opacity: float, color_hex: str,
                                   page_width: float, page_height: float,
                                   position: str = "diagonal", rotation: float = 45.0) -> bytes:
    """Create watermark PDF with custom rotation."""
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(page_width, page_height))

    try:
        r = int(color_hex[0:2], 16) / 255
        g_val = int(color_hex[2:4], 16) / 255
        b = int(color_hex[4:6], 16) / 255
    except Exception:
        r, g_val, b = 0.5, 0.5, 0.5

    alpha = max(0.05, min(opacity, 0.95))
    c.setFillColorRGB(r, g_val, b, alpha=alpha)
    font_size = min(page_width, page_height) * 0.08
    c.setFont("Helvetica-Bold", font_size)

    if position == "diagonal":
        c.saveState()
        c.translate(page_width / 2, page_height / 2)
        c.rotate(rotation)
        c.drawCentredString(0, 0, text)
        c.restoreState()
    elif position == "center":
        c.drawCentredString(page_width / 2, page_height / 2, text)
    elif position == "top":
        c.drawCentredString(page_width / 2, page_height * 0.95 - font_size, text)
    elif position == "bottom":
        c.drawCentredString(page_width / 2, page_height * 0.05, text)
    elif position == "tile":
        for row_i in range(3):
            for col_i in range(3):
                x = page_width * (col_i + 0.5) / 3
                y = page_height * (row_i + 0.5) / 3
                c.saveState()
                c.translate(x, y)
                c.rotate(rotation)
                c.drawCentredString(0, 0, text)
                c.restoreState()
    else:
        c.saveState()
        c.translate(page_width / 2, page_height / 2)
        c.rotate(rotation)
        c.drawCentredString(0, 0, text)
        c.restoreState()

    c.save()
    buf.seek(0)
    return buf.read()

@app.route("/api/page-numbers", methods=["POST"])
@rate_limited()
def page_numbers():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    position = request.form.get("position", "bottom")
    try:
        start = int(request.form.get("start", "1"))
    except ValueError:
        return err("start must be an integer")
    prefix = sanitize(request.form.get("prefix", ""), 50)
    try:
        with temp_upload(f) as path:
            doc = fitz.open(path)
            try:
                for i, page in enumerate(doc):
                    r = page.rect
                    label = f"{prefix}{start + i}"
                    pn = _make_page_num(label, position, r.width, r.height)
                    pnpdf = fitz.open("pdf", pn)
                    page.show_pdf_page(fitz.Rect(0, 0, r.width, r.height), pnpdf, 0, overlay=True)
                    pnpdf.close()
                filename = generate_output_filename(f.filename, "numbered")
                out = os.path.join(Config.OUTPUT_FOLDER, filename)
                doc.save(out)
            finally:
                doc.close()
        return ok("Page numbers added", out)
    except Exception:
        log.exception("page_numbers")
        return err("Page numbering failed", 500)


@app.route("/api/crop", methods=["POST"])
@rate_limited()
def crop_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)

    def safe_float(val, default=0.0):
        try:
            return float(val)
        except (ValueError, TypeError):
            return default

    left = safe_float(request.form.get("left", "0"))
    right = safe_float(request.form.get("right", "0"))
    top = safe_float(request.form.get("top", "0"))
    bottom = safe_float(request.form.get("bottom", "0"))

    try:
        with temp_upload(f) as path:
            doc = fitz.open(path)
            try:
                for page in doc:
                    r = page.rect
                    page.set_cropbox(fitz.Rect(r.x0 + left, r.y0 + top, r.x1 - right, r.y1 - bottom))
                filename = generate_output_filename(f.filename, "cropped")
                out = os.path.join(Config.OUTPUT_FOLDER, filename)
                doc.save(out)
            finally:
                doc.close()
        return ok("PDF pages cropped", out)
    except Exception:
        log.exception("crop")
        return err("Crop failed", 500)


@app.route("/api/info", methods=["POST"])
@rate_limited()
def pdf_info():
    """
    V7 UPGRADE: Rich metadata extraction including fonts, forms, TOC, images,
    linearization check, all page sizes summary, and raw file size.
    """
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            file_size_bytes = os.path.getsize(path)
            doc = fitz.open(path)
            try:
                meta = doc.metadata

                # Page sizes: collect all, return summary
                size_counts = {}
                for pg in doc:
                    w = round(pg.rect.width, 1)
                    h = round(pg.rect.height, 1)
                    key = (w, h)
                    size_counts[key] = size_counts.get(key, 0) + 1

                unique_sizes = [{"w": k[0], "h": k[1], "count": v}
                                for k, v in sorted(size_counts.items(), key=lambda x: -x[1])]
                all_same = len(unique_sizes) == 1

                # has_forms: any page has form fields (widgets)
                has_forms = any(pg.first_widget for pg in doc)

                # has_toc
                has_toc = len(doc.get_toc()) > 0

                # image_count
                image_count = sum(len(pg.get_images()) for pg in doc)

                # fonts_used: unique font basenames (up to 20)
                font_names = set()
                for pg in doc:
                    for font_info in pg.get_fonts(full=True):
                        basename = font_info[3] if len(font_info) > 3 else ""
                        if basename:
                            font_names.add(basename)
                fonts_used = sorted(font_names)[:20]

                # is_linearized: check first xref object for /Linearized key
                is_linearized = False
                try:
                    xref_str = doc.xref_object(1, compressed=False)
                    is_linearized = "/Linearized" in (xref_str or "")
                except Exception:
                    pass

                # pdf_version
                try:
                    pdf_version = doc.pdf_version()
                except Exception:
                    pdf_version = meta.get("format", "unknown")

                out_data = {
                    "page_count": len(doc),
                    "pdf_version": str(pdf_version),
                    "title": meta.get("title", ""),
                    "author": meta.get("author", ""),
                    "subject": meta.get("subject", ""),
                    "creator": meta.get("creator", ""),
                    "encrypted": doc.is_encrypted,
                    "file_size_bytes": file_size_bytes,
                    "size_human": (f"{file_size_bytes/1048576:.2f} MB"
                                   if file_size_bytes > 1048576
                                   else f"{file_size_bytes/1024:.1f} KB"),
                    "has_forms": has_forms,
                    "has_toc": has_toc,
                    "image_count": image_count,
                    "fonts_used": fonts_used,
                    "is_linearized": is_linearized,
                    "page_sizes": {
                        "unique_sizes": unique_sizes,
                        "all_same": all_same
                    }
                }
            finally:
                doc.close()
        return ok("PDF info retrieved", **out_data)
    except Exception:
        log.exception("pdf_info")
        return err("Info retrieval failed", 500)


# ═════════════════════════════════════════════════════════════════
# PDF SECURITY
# ═════════════════════════════════════════════════════════════════
@app.route("/api/protect", methods=["POST"])
@rate_limited()
def protect_pdf():
    """
    V7 UPGRADE: AES-256 encryption via PyMuPDF instead of RC4 via PyPDF2.
    Accepts allow_print and allow_copy boolean flags.
    """
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    pw = sanitize(request.form.get("password", ""))
    pw2 = sanitize(request.form.get("password2", ""))
    if not pw:
        return err("Password required")
    if pw != pw2:
        return err("Passwords do not match")

    allow_print = request.form.get("allow_print", "true").lower() in ("true", "1", "yes")
    allow_copy = request.form.get("allow_copy", "true").lower() in ("true", "1", "yes")

    try:
        with temp_upload(f) as path:
            doc = fitz.open(path)
            try:
                permissions = 0
                if allow_print:
                    permissions |= int(fitz.PDF_PERM_PRINT)
                if allow_copy:
                    permissions |= int(fitz.PDF_PERM_COPY)

                filename = generate_output_filename(f.filename, "protected")
                out = os.path.join(Config.OUTPUT_FOLDER, filename)
                doc.save(
                    out,
                    encryption=fitz.PDF_ENCRYPT_AES_256,
                    owner_pw=pw,
                    user_pw=pw,
                    permissions=permissions
                )
            finally:
                doc.close()
        return ok("PDF password protected with AES-256 encryption", out)
    except Exception:
        log.exception("protect")
        return err("Protect failed", 500)


@app.route("/api/unlock", methods=["POST"])
@rate_limited()
def unlock_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    pw = sanitize(request.form.get("password", ""))
    if not pw:
        return err("Password required")
    try:
        with temp_upload(f) as path:
            r = PdfReader(path, password=pw)
            w = PdfWriter()
            for page in r.pages:
                w.add_page(page)
            filename = generate_output_filename(f.filename, "unlocked")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(out, "wb") as fh:
                w.write(fh)
        return ok("PDF unlocked successfully", out)
    except Exception:
        log.exception("unlock")
        return err("Unlock failed — check password", 500)


@app.route("/api/sign-pdf", methods=["POST"])
@rate_limited()
def sign_pdf():
    """
    V7 UPGRADE: Image signature support, position control, page targeting.
    - signature: optional image file (PNG/JPG)
    - page: first|last|all|<integer 1-based> (default: last)
    - position: bottom-left|bottom-right|top-left|top-right|center (default: bottom-right)
    - name, reason: text metadata
    """
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)

    name = sanitize(request.form.get("name", "Signed"))
    reason = sanitize(request.form.get("reason", "Approved"))
    page_target = sanitize(request.form.get("page", "last"), 10)
    position = sanitize(request.form.get("position", "bottom-right"), 20)
    if position not in ("bottom-left", "bottom-right", "top-left", "top-right", "center"):
        position = "bottom-right"

    sig_file = request.files.get("signature")
    sig_data = None
    if sig_file and sig_file.filename:
        sig_data = sig_file.read()

    today_str = datetime.now().strftime('%Y-%m-%d')

    def _get_sig_pos(rect, pos):
        if pos == "bottom-right":
            return (rect.x1 - 180, rect.y1 - 70)
        elif pos == "bottom-left":
            return (rect.x0 + 30, rect.y1 - 70)
        elif pos == "top-right":
            return (rect.x1 - 180, rect.y0 + 50)
        elif pos == "top-left":
            return (rect.x0 + 30, rect.y0 + 50)
        elif pos == "center":
            return (rect.x0 + rect.width / 2 - 75, rect.y0 + rect.height / 2)
        return (rect.x1 - 180, rect.y1 - 70)

    try:
        with temp_upload(f) as path:
            doc = fitz.open(path)
            try:
                total = len(doc)

                # Determine which page indices to sign
                if page_target == "all":
                    page_indices = list(range(total))
                elif page_target == "first":
                    page_indices = [0]
                elif page_target == "last":
                    page_indices = [total - 1]
                else:
                    try:
                        pg_num = int(page_target)
                        idx = max(0, min(pg_num - 1, total - 1))
                        page_indices = [idx]
                    except ValueError:
                        page_indices = [total - 1]

                for pg_idx in page_indices:
                    page = doc[pg_idx]
                    rect = page.rect
                    sig_x, sig_y = _get_sig_pos(rect, position)

                    if sig_data:
                        # Insert signature image
                        img_rect = fitz.Rect(sig_x, sig_y - 40, sig_x + 150, sig_y + 5)
                        page.insert_image(img_rect, stream=sig_data, overlay=True)

                    # Always draw border box and text line
                    text_line = f"{name}  |  {reason}  |  {today_str}"
                    box_rect = fitz.Rect(sig_x - 5, sig_y - 5, sig_x + 155, sig_y + 25)
                    page.draw_rect(box_rect, color=(0, 0, 0.6), fill=(0.9, 0.9, 1), width=0.5)
                    page.insert_text((sig_x, sig_y + 12), text_line, fontsize=8, color=(0, 0, 0.5))

                filename = generate_output_filename(f.filename, "signed")
                out = os.path.join(Config.OUTPUT_FOLDER, filename)
                doc.save(out)
            finally:
                doc.close()
        return ok(f"Signature added to {len(page_indices)} page(s) at {position}", out)
    except Exception:
        log.exception("sign_pdf")
        return err("Sign failed", 500)


@app.route("/api/redact-pdf", methods=["POST"])
@rate_limited()
def redact_pdf():
    """
    V7 UPGRADE: Supports text, regex, and preset redaction modes.
    - mode: text|regex|preset (default: text)
    - search_text: text to search (mode=text)
    - pattern: regex pattern (mode=regex)
    - preset: ssn|email|phone|aadhaar|pan|credit_card (mode=preset)
    """
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)

    mode = sanitize(request.form.get("mode", "text"), 10)
    if mode not in ("text", "regex", "preset"):
        mode = "text"

    PRESET_PATTERNS = {
        "ssn":         r'\b\d{3}-\d{2}-\d{4}\b',
        "email":       r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b',
        "phone":       r'\b[\d\s\-\(\)]{10,15}\b',
        "aadhaar":     r'\b\d{4}\s?\d{4}\s?\d{4}\b',
        "pan":         r'\b[A-Z]{5}\d{4}[A-Z]\b',
        "credit_card": r'\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b',
    }

    pattern_str = None
    search_text = None

    if mode == "text":
        search_text = sanitize(request.form.get("search_text", ""))
        if not search_text:
            return err("search_text required for mode=text")

    elif mode == "regex":
        pattern_str = sanitize(request.form.get("pattern", ""), 500)
        if not pattern_str:
            return err("pattern required for mode=regex")
        try:
            compiled = re.compile(pattern_str)
        except re.error as rex:
            return err(f"Invalid regex pattern: {rex}")

    elif mode == "preset":
        preset_name = sanitize(request.form.get("preset", ""), 30)
        if preset_name not in PRESET_PATTERNS:
            return err(f"Unknown preset. Choose from: {', '.join(PRESET_PATTERNS.keys())}")
        pattern_str = PRESET_PATTERNS[preset_name]
        compiled = re.compile(pattern_str)

    try:
        with temp_upload(f) as path:
            doc = fitz.open(path)
            try:
                count = 0
                for page in doc:
                    if mode == "text":
                        hits = page.search_for(search_text)
                        for rect in hits:
                            page.add_redact_annot(rect, fill=(0, 0, 0))
                            count += 1
                    else:
                        # regex or preset: get full page text, find matches, search for each
                        page_text = page.get_text("text")
                        matches = list(compiled.finditer(page_text))
                        for match in matches:
                            matched_str = match.group()
                            rects = page.search_for(matched_str)
                            for rect in rects:
                                page.add_redact_annot(rect, fill=(0, 0, 0))
                                count += 1
                    page.apply_redactions()

                filename = generate_output_filename(f.filename, "redacted")
                out = os.path.join(Config.OUTPUT_FOLDER, filename)
                doc.save(out)
            finally:
                doc.close()
        return ok(f"Redacted {count} occurrence(s) (mode={mode})", out, redaction_count=count)
    except Exception:
        log.exception("redact")
        return err("Redact failed", 500)


@app.route("/api/compare-pdf", methods=["POST"])
@rate_limited()
def compare_pdf():
    """
    V7 UPGRADE: Adds text diff summary JSON alongside visual diff images.
    Returns ZIP containing:
    - diff_page_NNNN.png (visual pixel diff, enhanced)
    - text_diff_summary.json (word-level diff per page)
    """
    files = request.files.getlist("files")
    if len(files) != 2:
        return err("Exactly 2 PDF files required for comparison")
    for f in files:
        e = validate_file(f, Config.ALLOWED_PDF)
        if e:
            return err(e)
    try:
        with temp_uploads(files) as paths:
            doc1 = fitz.open(paths[0])
            doc2 = fitz.open(paths[1])
            try:
                pages = min(len(doc1), len(doc2))
                buf = io.BytesIO()
                text_diff_pages = []
                overall_similarities = []

                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for i in range(pages):
                        # Visual diff
                        pix1 = doc1[i].get_pixmap(dpi=150)
                        pix2 = doc2[i].get_pixmap(dpi=150)
                        img1 = Image.open(io.BytesIO(pix1.tobytes("png"))).convert("RGB")
                        img2 = Image.open(io.BytesIO(pix2.tobytes("png"))).convert("RGB")
                        if img1.size != img2.size:
                            img2 = img2.resize(img1.size, Image.LANCZOS)
                        diff = ImageChops.difference(img1, img2)
                        diff_enhanced = diff.point(lambda x: min(x * 8, 255))
                        diff_out = io.BytesIO()
                        diff_enhanced.save(diff_out, format="PNG")
                        zf.writestr(f"diff_page_{i+1:04d}.png", diff_out.getvalue())

                        # Text diff
                        words1 = [w[4] for w in doc1[i].get_text("words")]
                        words2 = [w[4] for w in doc2[i].get_text("words")]
                        sm = difflib.SequenceMatcher(None, words1, words2)
                        similarity = round(sm.ratio() * 100, 1)
                        overall_similarities.append(similarity)

                        added = []
                        removed = []
                        for tag, i1, i2, j1, j2 in sm.get_opcodes():
                            if tag == "insert":
                                added.extend(words2[j1:j2])
                            elif tag == "delete":
                                removed.extend(words1[i1:i2])
                            elif tag == "replace":
                                removed.extend(words1[i1:i2])
                                added.extend(words2[j1:j2])

                        text_diff_pages.append({
                            "page": i + 1,
                            "similarity_pct": similarity,
                            "words_added": added[:100],   # cap for large diffs
                            "words_removed": removed[:100]
                        })

                    overall_sim = round(sum(overall_similarities) / len(overall_similarities), 1) if overall_similarities else 0.0
                    text_summary = {
                        "pages": text_diff_pages,
                        "overall_similarity_pct": overall_sim
                    }
                    zf.writestr("text_diff_summary.json", json.dumps(text_summary, ensure_ascii=False, indent=2))

            finally:
                doc1.close()
                doc2.close()

            filename = generate_output_filename(
                files[0].filename, "comparison",
                is_multi=True, filenames=[f.filename for f in files]
            )
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(out, "wb") as fh:
                fh.write(buf.getvalue())
        return ok(f"Compared {pages} page(s) — visual diffs + text diff summary included", out)
    except Exception:
        log.exception("compare_pdf")
        return err("Comparison failed", 500)


# ═════════════════════════════════════════════════════════════════
# PDF CONVERT FROM
# ═════════════════════════════════════════════════════════════════
@app.route("/api/pdf-to-image", methods=["POST"])
@rate_limited()
def pdf_to_image():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    fmt = request.form.get("format", "jpg").lower()
    try:
        dpi = min(int(request.form.get("dpi", "150")), 300)
    except ValueError:
        return err("dpi must be an integer")
    if fmt not in ("jpg", "png"):
        fmt = "jpg"
    try:
        with temp_upload(f) as path:
            doc = fitz.open(path)
            try:
                count = len(doc)
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for i, page in enumerate(doc):
                        mat = fitz.Matrix(dpi / 72, dpi / 72)
                        pix = page.get_pixmap(matrix=mat, alpha=False)
                        zf.writestr(f"page_{i+1:04d}.{fmt}", pix.tobytes(fmt))
            finally:
                doc.close()
            filename = generate_output_filename(f.filename, "to_image")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(out, "wb") as fh:
                fh.write(buf.getvalue())
        return ok(f"Exported {count} page(s) as {fmt.upper()}", out)
    except Exception:
        log.exception("pdf_to_image")
        return err("Export failed", 500)


# ═════════════════════════════════════════════════════════════════
# PDF TO WORD
# ═════════════════════════════════════════════════════════════════
@app.route("/api/pdf-to-word", methods=["POST"])
@rate_limited()
def pdf_to_word():
    if not PDF2DOCX_AVAILABLE:
        return err("PDF to Word requires pdf2docx. Install: pip install pdf2docx", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)

    try:
        file_size = 0
        f.seek(0, 2)
        file_size = f.tell()
        f.seek(0)

        ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else "pdf"
        upload_path = os.path.join(Config.UPLOAD_FOLDER, f"{uuid.uuid4()}.{ext}")
        f.save(upload_path)

        filename = generate_output_filename(f.filename, "to_word")
        filename = re.sub(r'\.pdf$', '.docx', filename, flags=re.IGNORECASE)
        if not filename.endswith('.docx'):
            filename = Path(filename).stem + '.docx'
        out = os.path.join(Config.OUTPUT_FOLDER, filename)

        SYNC_THRESHOLD = 5 * 1024 * 1024

        if file_size <= SYNC_THRESHOLD:
            try:
                cv = Pdf2DocxConverter(upload_path)
                cv.convert(out, start=0, end=None)
                cv.close()
            finally:
                try:
                    os.remove(upload_path)
                except Exception:
                    pass

            if not os.path.exists(out) or os.path.getsize(out) == 0:
                return err("Conversion failed — output file is empty or missing", 500)

            return ok("PDF converted to Word successfully", out)

        else:
            job_id = str(uuid.uuid4())
            with _job_store_lock:
                _job_store[job_id] = {
                    "status": "pending",
                    "filename": filename,
                    "out": out,
                    "created_at": time.time(),
                    "error": None
                }

            def _convert_bg():
                try:
                    cv = Pdf2DocxConverter(upload_path)
                    cv.convert(out, start=0, end=None)
                    cv.close()
                    if os.path.exists(out) and os.path.getsize(out) > 0:
                        with _job_store_lock:
                            if job_id in _job_store:
                                _job_store[job_id]["status"] = "done"
                    else:
                        raise RuntimeError("Output file missing or empty after conversion")
                except Exception as ex:
                    log.error(f"pdf-to-word job {job_id} failed: {ex}")
                    with _job_store_lock:
                        if job_id in _job_store:
                            _job_store[job_id]["status"] = "error"
                            _job_store[job_id]["error"] = str(ex)
                finally:
                    try:
                        os.remove(upload_path)
                    except Exception:
                        pass

            t = threading.Thread(target=_convert_bg, daemon=True)
            t.start()

            return jsonify({
                "success": True,
                "message": "PDF to Word conversion started. Poll the status endpoint.",
                "job_id": job_id,
                "status_url": f"/api/pdf-to-word/status/{job_id}",
                "poll_interval_ms": 2000
            })

    except Exception:
        log.exception("pdf_to_word")
        return err("PDF to Word failed", 500)


@app.route("/api/pdf-to-word/status/<job_id>", methods=["GET"])
@rate_limited()
def pdf_to_word_status(job_id: str):
    """Returns job status with progress percentage."""
    safe_id = re.sub(r'[^a-f0-9\-]', '', job_id)
    if safe_id != job_id:
        return err("Invalid job ID", 400)
    
    # Check in-memory store first
    with _job_store_lock:
        job = _job_store.get(job_id)
    
    if job:
        status = job["status"]
        if status == "done":
            out = job["out"]
            if not os.path.exists(out):
                return err("Output file missing", 404)
            fname = os.path.basename(out)
            size = os.path.getsize(out)
            return jsonify({
                "success": True,
                "status": "done",
                "progress_pct": 100,
                "download_url": f"/download/{fname}",
                "filename": fname,
                "size_human": f"{size/1048576:.2f} MB" if size > 1048576 else f"{size/1024:.1f} KB"
            })
        elif status == "error":
            return jsonify({
                "success": False,
                "status": "error",
                "error": job.get("error", "Conversion failed")
            }), 500
        else:
            elapsed = round(time.time() - job.get("created_at", time.time()), 1)
            return jsonify({
                "success": True,
                "status": "pending",
                "progress_pct": 0,
                "elapsed_seconds": elapsed
            })
    
    # Check Celery task
    from celery.result import AsyncResult
    task = AsyncResult(job_id, app=celery)
    
    if task.state == 'PENDING':
        return jsonify({"success": True, "status": "pending", "progress_pct": 0})
    elif task.state == 'PROGRESS':
        meta = task.info or {}
        current = meta.get('current', 0)
        total = meta.get('total', 1)
        progress = int((current / total) * 100) if total > 0 else 0
        return jsonify({
            "success": True,
            "status": "processing",
            "progress_pct": progress,
            "current": current,
            "total": total
        })
    elif task.state == 'SUCCESS':
        result = task.result
        return jsonify({
            "success": True,
            "status": "done",
            "progress_pct": 100,
            "download_url": f"/download/{os.path.basename(result.get('output', ''))}",
            "filename": os.path.basename(result.get('output', ''))
        })
    elif task.state == 'FAILURE':
        return jsonify({
            "success": False,
            "status": "error",
            "error": str(task.info)
        }), 500
    
    return err("Job not found", 404)

@app.route("/api/pdf-to-excel", methods=["POST"])
@rate_limited()
def pdf_to_excel():
    """
    V7.1 UPGRADE: pdfplumber primary + Tabula fallback + raw text extraction.
    Extracts tables with industry-leading accuracy.
    """
    if not OPENPYXL_AVAILABLE:
        return err("PDF to Excel requires openpyxl. Install: pip install openpyxl", 501)
    
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    
    try:
        with temp_upload(f) as path:
            wb = Workbook()
            wb.remove(wb.active)
            tables_extracted = 0
            method_used = None
            
            # PRIMARY: pdfplumber (most accurate)
            try:
                import pdfplumber
                with pdfplumber.open(path) as pdf:
                    for page_num, page in enumerate(pdf.pages):
                        # Extract tables with advanced settings
                        page_tables = page.extract_tables({
                            "vertical_strategy": "lines",
                            "horizontal_strategy": "lines",
                            "snap_tolerance": 3,
                        })
                        for table_num, table in enumerate(page_tables):
                            if table and any(any(cell for cell in row if cell) for row in table):
                                tables_extracted += 1
                                ws = wb.create_sheet(title=f"Table_{tables_extracted}")
                                # Clean and write table
                                for row in table:
                                    cleaned_row = []
                                    for cell in row:
                                        if cell is None:
                                            cleaned_row.append("")
                                        else:
                                            cleaned_row.append(str(cell).strip())
                                    if any(cleaned_row):  # Skip completely empty rows
                                        ws.append(cleaned_row)
                                
                                # Format header row
                                if ws.max_row > 0:
                                    for cell in ws[1]:
                                        cell.font = Font(bold=True)
                
                if tables_extracted > 0:
                    method_used = "pdfplumber"
            except ImportError:
                log.warning("pdfplumber not installed, falling back to Tabula")
            except Exception as e:
                log.warning(f"pdfplumber failed: {e}, falling back to Tabula")
            
            # FALLBACK: Tabula
            if tables_extracted == 0 and TABULA_AVAILABLE:
                try:
                    dfs = tabula.read_pdf(path, pages='all', multiple_tables=True, lattice=True)
                    for i, df in enumerate(dfs):
                        if not df.empty:
                            tables_extracted += 1
                            ws = wb.create_sheet(title=f"Table_{i+1}")
                            ws.append(list(df.columns))
                            for row in df.itertuples(index=False):
                                ws.append([str(v) if v is not None else "" for v in row])
                    method_used = "tabula"
                except Exception as tabula_ex:
                    log.warning(f"Tabula failed: {tabula_ex}")
            
            # ULTIMATE FALLBACK: Raw text extraction
            if tables_extracted == 0:
                ws = wb.create_sheet(title="Extracted_Text")
                ws['A1'] = "No tables detected. Full text extraction:"
                ws['A1'].font = Font(bold=True, size=12)
                
                doc = fitz.open(path)
                row_idx = 3
                for page_num, page in enumerate(doc):
                    ws[f'A{row_idx}'] = f"--- Page {page_num + 1} ---"
                    ws[f'A{row_idx}'].font = Font(bold=True)
                    row_idx += 1
                    
                    text = page.get_text("text")
                    for line in text.split('\n'):
                        if line.strip():
                            ws[f'A{row_idx}'] = line.strip()
                            row_idx += 1
                doc.close()
                method_used = "raw_text_extraction"
            
            filename = generate_output_filename(f.filename, "to_excel")
            filename = re.sub(r'\.pdf$', '.xlsx', filename, flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            wb.save(out)
            
        return ok(
            f"Extracted {tables_extracted} table(s) to Excel (method: {method_used})",
            out,
            tables_found=tables_extracted,
            extraction_method=method_used
        )
    except Exception:
        log.exception("pdf_to_excel")
        return err("PDF to Excel failed", 500)


@app.route("/api/pdf-to-ppt", methods=["POST"])
@rate_limited()
def pdf_to_ppt():
    """
    NOTE: PDF to editable PPT is technically impossible without a full layout engine.
    This tool renders each PDF page as a high-resolution image on a slide.
    For editable text, use PDF to Word then import to PowerPoint.
    """
    if not PPTX_AVAILABLE:
        return err("PDF to PPT requires python-pptx. Install: pip install python-pptx", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            doc = fitz.open(path)
            try:
                prs = Presentation()
                prs.slide_width = PptxInches(10)
                prs.slide_height = PptxInches(7.5)
                blank = prs.slide_layouts[6]
                
                for page in doc:
                    # Use higher DPI for better quality
                    pix = page.get_pixmap(dpi=200)
                    tmp_img = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                    tmp_img.write(pix.tobytes("png"))
                    tmp_img.close()
                    slide = prs.slides.add_slide(blank)
                    slide.shapes.add_picture(tmp_img.name, 0, 0, prs.slide_width, prs.slide_height)
                    os.unlink(tmp_img.name)
            finally:
                doc.close()
            filename = generate_output_filename(f.filename, "to_ppt")
            filename = re.sub(r'\.pdf$', '.pptx', filename, flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            prs.save(out)
        return ok(
            "PDF converted to PowerPoint (pages as images). For editable text, use PDF to Word first.",
            out,
            note="Pages rendered as high-resolution images"
        )
    except Exception:
        log.exception("pdf_to_ppt")
        return err("PDF to PPT failed", 500)

@app.route("/api/pdf-to-pdfa", methods=["POST"])
@rate_limited()
def pdf_to_pdfa():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    version = request.form.get("version", "1b")
    pdfa_val = "2" if "3" in version else "1"
    try:
        with temp_upload(f) as path:
            filename = generate_output_filename(f.filename, "pdfa")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            cmd = [
                Config.GHOSTSCRIPT, "-dBATCH", "-dNOPAUSE", "-dNOSAFER",
                "-sDEVICE=pdfwrite", f"-dPDFA={pdfa_val}", "-dPDFACompatibilityPolicy=1",
                f"-sOutputFile={out}", path
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=6000)
            if result.returncode != 0:
                return err("Ghostscript PDF/A conversion failed. Is Ghostscript installed?", 500)
        return ok(f"Converted to PDF/A-{version}", out)
    except subprocess.TimeoutExpired:
        return err("PDF/A conversion timed out", 500)
    except Exception:
        log.exception("pdf_to_pdfa")
        return err("PDF/A conversion failed", 500)


# ═════════════════════════════════════════════════════════════════
# PDF CONVERT TO (images → PDF)
# ═════════════════════════════════════════════════════════════════
def _images_to_pdf(paths: list, page_size_str: str, output_filename: str) -> str:
    size_map = {"a4": A4, "letter": letter}
    size = size_map.get(page_size_str.lower(), None)
    out = os.path.join(Config.OUTPUT_FOLDER, output_filename)
    c = rl_canvas.Canvas(out, pagesize=size or letter)
    for path in paths:
        try:
            img = Image.open(path)
            iw, ih = img.size
            if size:
                pw, ph = size
            else:
                pw, ph = iw * 72 / 96, ih * 72 / 96
            sw = min(pw * 0.95, iw * 72 / 96)
            sh = sw * ih / iw
            if sh > ph * 0.95:
                sh = ph * 0.95
                sw = sh * iw / ih
            x = (pw - sw) / 2
            y = (ph - sh) / 2
            c._pagesize = (pw, ph)
            c.drawImage(path, x, y, width=sw, height=sh)
            c.showPage()
        except Exception as ex:
            log.warning(f"Skipping image {path}: {ex}")
    c.save()
    return out


@app.route("/api/image-to-pdf", methods=["POST"])
@rate_limited()
def image_to_pdf():
    files = request.files.getlist("files")
    if not files or all(f.filename == "" for f in files):
        return err("At least one image file required")
    for f in files:
        e = validate_file(f, Config.ALLOWED_IMAGE)
        if e:
            return err(e)
    page_size = request.form.get("page_size", "auto")
    try:
        with temp_uploads(files) as paths:
            filename = generate_output_filename(
                files[0].filename, "to_pdf",
                is_multi=True, filenames=[f.filename for f in files]
            )
            filename = re.sub(r'\.(jpg|jpeg|png|gif|bmp|tiff|webp)$', '.pdf',
                               filename, flags=re.IGNORECASE)
            if not filename.endswith('.pdf'):
                filename = Path(filename).stem + '.pdf'
            out = _images_to_pdf(paths, page_size, filename)
        return ok(f"Converted {len(files)} image(s) to PDF", out)
    except Exception:
        log.exception("image_to_pdf")
        return err("Image to PDF failed", 500)


@app.route("/api/jpg-to-pdf", methods=["POST"])
@rate_limited()
def jpg_to_pdf():
    files = request.files.getlist("files")
    if not files or all(f.filename == "" for f in files):
        return err("At least one JPG file required")
    for f in files:
        e = validate_file(f, Config.ALLOWED_JPG)
        if e:
            return err(e)
    page_size = request.form.get("page_size", "auto")
    try:
        with temp_uploads(files) as paths:
            filename = generate_output_filename(
                files[0].filename, "to_pdf",
                is_multi=True, filenames=[f.filename for f in files]
            )
            filename = re.sub(r'\.(jpg|jpeg)$', '.pdf', filename, flags=re.IGNORECASE)
            if not filename.endswith('.pdf'):
                filename = Path(filename).stem + '.pdf'
            out = _images_to_pdf(paths, page_size, filename)
        return ok(f"Converted {len(files)} JPG(s) to PDF", out)
    except Exception:
        log.exception("jpg_to_pdf")
        return err("JPG to PDF failed", 500)


@app.route("/api/word-to-pdf", methods=["POST"])
@rate_limited()
def word_to_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            filename = generate_output_filename(f.filename, "to_pdf")
            filename = re.sub(r'\.(doc|docx)$', '.pdf', filename, flags=re.IGNORECASE)
            out = libre(path, "pdf", output_filename=filename)
            if not out:
                return err("LibreOffice conversion failed. Ensure LibreOffice is installed.", 500)
        return ok("Word converted to PDF", out)
    except Exception:
        log.exception("word_to_pdf")
        return err("Word to PDF failed", 500)


@app.route("/api/excel-to-pdf", methods=["POST"])
@rate_limited()
def excel_to_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            filename = generate_output_filename(f.filename, "to_pdf")
            filename = re.sub(r'\.(xls|xlsx)$', '.pdf', filename, flags=re.IGNORECASE)
            out = libre(path, "pdf", output_filename=filename)
            if not out:
                return err("LibreOffice conversion failed. Ensure LibreOffice is installed.", 500)
        return ok("Excel converted to PDF", out)
    except Exception:
        log.exception("excel_to_pdf")
        return err("Excel to PDF failed", 500)


@app.route("/api/html-to-pdf", methods=["POST"])
@rate_limited()
def html_to_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_HTML)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            filename = generate_output_filename(f.filename, "to_pdf")
            filename = re.sub(r'\.(html|htm)$', '.pdf', filename, flags=re.IGNORECASE)
            out_path = os.path.join(Config.OUTPUT_FOLDER, filename)

            result = subprocess.run(
                ["wkhtmltopdf", "--quiet", path, out_path],
                capture_output=True, timeout=60
            )
            if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return ok("HTML converted to PDF", out_path)

            try:
                from weasyprint import HTML
                HTML(filename=path).write_pdf(out_path)
                if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    return ok("HTML converted to PDF", out_path)
            except ImportError:
                pass
            except Exception as weasy_error:
                log.warning(f"WeasyPrint failed: {weasy_error}")

            return err("HTML to PDF conversion failed", 500)

    except subprocess.TimeoutExpired:
        return err("HTML to PDF timed out", 500)
    except Exception:
        log.exception("html_to_pdf")
        return err("HTML to PDF failed", 500)


# ═════════════════════════════════════════════════════════════════
# IMAGE TOOLS
# ═════════════════════════════════════════════════════════════════
@app.route("/api/compress-image", methods=["POST"])
@rate_limited()
def compress_image():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_IMAGE)
    if e:
        return err(e)
    try:
        quality = min(max(int(request.form.get("quality", "75")), 1), 95)
    except ValueError:
        return err("quality must be an integer between 1 and 95")
    output_format = request.form.get("output_format", "auto").lower()

    try:
        with temp_upload(f) as path:
            orig_size = os.path.getsize(path)
            img = Image.open(path)
            ext = path.rsplit(".", 1)[-1].lower()

            if output_format == "webp":
                target_fmt = "webp"
            elif output_format in ("jpg", "jpeg"):
                target_fmt = "jpeg"
            elif output_format == "png":
                target_fmt = "png"
            elif ext == "png":
                target_fmt = "png"
            elif ext == "webp":
                target_fmt = "webp"
            else:
                target_fmt = "jpeg"

            filename = generate_output_filename(f.filename, "compressed")
            filename = re.sub(r'\.\w+$', f'.{target_fmt if target_fmt != "jpeg" else "jpg"}', filename)
            out = os.path.join(Config.OUTPUT_FOLDER, filename)

            if target_fmt == "png":
                if img.mode not in ("RGB", "RGBA", "L", "LA", "P"):
                    img = img.convert("RGBA")
                tmp_png = path + "_tmp.png"
                img.save(tmp_png, format="PNG", optimize=False)
                pngquant_ok = False
                try:
                    result = subprocess.run(
                        ["pngquant", "--quality", f"{max(1,quality-15)}-{quality}",
                         "--speed", "3", "--force", "--output", out, tmp_png],
                        capture_output=True, timeout=30
                    )
                    if result.returncode in (0, 99) and os.path.exists(out) and os.path.getsize(out) > 0:
                        pngquant_ok = True
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    pass
                finally:
                    try:
                        os.remove(tmp_png)
                    except Exception:
                        pass
                if not pngquant_ok:
                    img.save(out, format="PNG", optimize=True, compress_level=9)

            elif target_fmt == "webp":
                if img.mode not in ("RGB", "RGBA"):
                    img = img.convert("RGB")
                img.save(out, format="WEBP", quality=quality, method=6, optimize=True)

            else:
                if img.mode in ("RGBA", "P", "LA"):
                    bg = Image.new("RGB", img.size, (255, 255, 255))
                    if img.mode == "P":
                        img = img.convert("RGBA")
                    mask = img.split()[-1] if img.mode in ("RGBA", "LA") else None
                    bg.paste(img, mask=mask)
                    img = bg
                elif img.mode != "RGB":
                    img = img.convert("RGB")
                img.save(out, format="JPEG", quality=quality, optimize=True, progressive=True, exif=b"")

            new_size = os.path.getsize(out)
            reduction = round((1 - new_size / orig_size) * 100, 1) if orig_size else 0

        return ok(
            f"Image compressed {reduction}% smaller as {target_fmt.upper()}",
            out,
            reduction_pct=reduction,
            output_format=target_fmt
        )
    except Exception:
        log.exception("compress_image")
        return err("Image compression failed", 500)


@app.route("/api/resize-image", methods=["POST"])
@rate_limited()
def resize_image():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_IMAGE)
    if e:
        return err(e)
    try:
        width = int(request.form.get("width", "800"))
        height = int(request.form.get("height", "600"))
    except ValueError:
        return err("width and height must be integers")
    keep_ratio = request.form.get("keep_ratio", "true").lower() in ("true", "on", "1", "yes")
    try:
        with temp_upload(f) as path:
            img = Image.open(path)
            if keep_ratio:
                img.thumbnail((width, height), Image.LANCZOS)
            else:
                img = img.resize((width, height), Image.LANCZOS)
            ext = path.rsplit(".", 1)[-1].lower()
            fmt = "JPEG" if ext in ("jpg", "jpeg") else "PNG"
            filename = generate_output_filename(f.filename, "resized")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            if fmt == "JPEG" and img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(out, format=fmt)
        return ok(f"Image resized to {img.size[0]}×{img.size[1]}", out)
    except Exception:
        log.exception("resize_image")
        return err("Resize failed", 500)


@app.route("/api/webp-to-jpg", methods=["POST"])
@rate_limited()
def webp_to_jpg():
    files = request.files.getlist("files")
    if not files or all(f.filename == "" for f in files):
        return err("At least one WebP file required")
    for f in files:
        e = validate_file(f, Config.ALLOWED_WEBP)
        if e:
            return err(e)
    try:
        quality = int(request.form.get("quality", "75"))
    except ValueError:
        return err("quality must be an integer")
    try:
        with temp_uploads(files) as paths:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, path in enumerate(paths):
                    img = Image.open(path).convert("RGB")
                    ib = io.BytesIO()
                    img.save(ib, format="JPEG", quality=quality)
                    zf.writestr(f"image_{i+1:04d}.jpg", ib.getvalue())
            filename = generate_output_filename(
                files[0].filename, "to_jpg",
                is_multi=True, filenames=[f.filename for f in files]
            )
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(out, "wb") as fh:
                fh.write(buf.getvalue())
        return ok(f"Converted {len(files)} WebP(s) to JPG", out)
    except Exception:
        log.exception("webp_to_jpg")
        return err("WebP to JPG failed", 500)


@app.route("/api/png-to-jpg", methods=["POST"])
@rate_limited()
def png_to_jpg():
    files = request.files.getlist("files")
    if not files or all(f.filename == "" for f in files):
        return err("At least one PNG file required")
    for f in files:
        e = validate_file(f, Config.ALLOWED_PNG)
        if e:
            return err(e)
    try:
        quality = int(request.form.get("quality", "75"))
    except ValueError:
        return err("quality must be an integer")
    try:
        with temp_uploads(files) as paths:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, path in enumerate(paths):
                    img = Image.open(path).convert("RGB")
                    ib = io.BytesIO()
                    img.save(ib, format="JPEG", quality=quality)
                    zf.writestr(f"image_{i+1:04d}.jpg", ib.getvalue())
            filename = generate_output_filename(
                files[0].filename, "to_jpg",
                is_multi=True, filenames=[f.filename for f in files]
            )
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(out, "wb") as fh:
                fh.write(buf.getvalue())
        return ok(f"Converted {len(files)} PNG(s) to JPG", out)
    except Exception:
        log.exception("png_to_jpg")
        return err("PNG to JPG failed", 500)


@app.route("/api/image-to-word", methods=["POST"])
@rate_limited()
def image_to_word():
    """
    V7 UPGRADE: Supports OCR mode.
    - mode: embed|ocr (default: embed)
    - mode=embed: insert image into Word document (original behavior)
    - mode=ocr: extract text via pytesseract, build Word doc with paragraphs
    - lang: tesseract language code (default: eng)
    """
    if not DOCX_AVAILABLE:
        return err("Image to Word requires python-docx. Install: pip install python-docx", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_IMAGE)
    if e:
        return err(e)

    mode = sanitize(request.form.get("mode", "embed"), 10)
    if mode not in ("embed", "ocr"):
        mode = "embed"

    raw_lang = request.form.get("lang", "eng")
    lang = re.sub(r'[^a-zA-Z0-9+\-]', '', raw_lang)[:50] or "eng"

    try:
        with temp_upload(f) as path:
            if mode == "ocr":
                if not TESSERACT_AVAILABLE:
                    return err("OCR mode requires pytesseract. Install: pip install pytesseract", 501)

                img = Image.open(path)

                # Extract text
                ocr_text = pytesseract.image_to_string(img, lang=lang, config="--psm 3 --oem 3")

                # Parse into paragraphs
                raw_paragraphs = re.split(r'\n{2,}', ocr_text.strip())
                word_count = len(ocr_text.split())

                doc = DocxDocument()

                # Embed thumbnail at top
                thumb = img.copy()
                thumb.thumbnail((400, 400), Image.LANCZOS)
                thumb_buf = io.BytesIO()
                thumb_fmt = "PNG" if img.format in (None, "PNG", "GIF") else "JPEG"
                thumb.save(thumb_buf, format=thumb_fmt)
                thumb_buf.seek(0)
                thumb_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                thumb_tmp.write(thumb_buf.getvalue())
                thumb_tmp.close()
                doc.add_picture(thumb_tmp.name, width=Inches(4))
                os.unlink(thumb_tmp.name)

                doc.add_paragraph()  # spacer

                for para_text in raw_paragraphs:
                    lines = para_text.strip().splitlines()
                    for line in lines:
                        line = line.strip()
                        if not line:
                            continue
                        # Detect heading: ALL CAPS or short line (< 50 chars)
                        is_heading = (line.isupper() and len(line) > 2) or \
                                     (len(line) < 50 and line == line.strip() and
                                      not line.endswith(('.', ',', ';')))
                        if is_heading:
                            doc.add_heading(line, level=1)
                        else:
                            doc.add_paragraph(line)

                filename = generate_output_filename(f.filename, "to_word")
                filename = re.sub(r'\.(jpg|jpeg|png|gif|bmp|tiff|webp)$', '.docx',
                                   filename, flags=re.IGNORECASE)
                out = os.path.join(Config.OUTPUT_FOLDER, filename)
                doc.save(out)
                return ok(f"Image OCR complete — {word_count} words extracted", out, word_count=word_count)

            else:
                # embed mode (original behavior)
                doc = DocxDocument()
                doc.add_heading("Converted Image", 0)
                doc.add_picture(path, width=Inches(6))
                filename = generate_output_filename(f.filename, "to_word")
                filename = re.sub(r'\.(jpg|jpeg|png|gif|bmp|tiff|webp)$', '.docx',
                                   filename, flags=re.IGNORECASE)
                out = os.path.join(Config.OUTPUT_FOLDER, filename)
                doc.save(out)
                return ok("Image inserted into Word document", out)

    except Exception:
        log.exception("image_to_word")
        return err("Image to Word failed", 500)


@app.route("/api/image-to-excel", methods=["POST"])
@rate_limited()
def image_to_excel():
    """
    V7 UPGRADE: OCR table extraction from images.
    Uses pytesseract word bounding boxes to detect grid structure.
    Falls back to image embed if OCR finds fewer than 3 words.
    - lang: tesseract language code (default: eng)
    """
    if not OPENPYXL_AVAILABLE:
        return err("Image to Excel requires openpyxl. Install: pip install openpyxl", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_IMAGE)
    if e:
        return err(e)

    raw_lang = request.form.get("lang", "eng")
    lang = re.sub(r'[^a-zA-Z0-9+\-]', '', raw_lang)[:50] or "eng"

    try:
        with temp_upload(f) as path:
            img = Image.open(path).convert("RGB")

            ocr_grid = None
            warning_msg = None

            if TESSERACT_AVAILABLE:
                try:
                    data = pytesseract.image_to_data(
                        img, lang=lang, output_type=TesseractOutput.DICT,
                        config="--psm 6"
                    )

                    # Filter valid words with confidence >= 30
                    words = []
                    for i in range(len(data["text"])):
                        word = (data["text"][i] or "").strip()
                        conf = int(data["conf"][i]) if str(data["conf"][i]) != "-1" else 0
                        if word and conf >= 30:
                            words.append({
                                "text": word,
                                "left": data["left"][i],
                                "top": data["top"][i],
                                "width": data["width"][i],
                                "height": data["height"][i],
                            })

                    if len(words) >= 3:
                        # Group words into rows by Y coordinate (tolerance ±8px)
                        row_tolerance = 8
                        rows_dict = {}
                        for w in words:
                            mid_y = w["top"] + w["height"] // 2
                            matched_key = None
                            for key in rows_dict:
                                if abs(key - mid_y) <= row_tolerance:
                                    matched_key = key
                                    break
                            if matched_key is None:
                                matched_key = mid_y
                                rows_dict[matched_key] = []
                            rows_dict[matched_key].append(w)

                        # Sort rows by Y, words within row by X
                        sorted_rows = []
                        for key in sorted(rows_dict.keys()):
                            row_words = sorted(rows_dict[key], key=lambda w: w["left"])
                            sorted_rows.append(row_words)

                        # Group words in each row into columns by X gap (>30px = new col)
                        col_gap_threshold = 30
                        grid = []
                        for row_words in sorted_rows:
                            row_cells = []
                            current_cell = row_words[0]["text"]
                            for wi in range(1, len(row_words)):
                                prev = row_words[wi - 1]
                                curr = row_words[wi]
                                gap = curr["left"] - (prev["left"] + prev["width"])
                                if gap > col_gap_threshold:
                                    row_cells.append(current_cell)
                                    current_cell = curr["text"]
                                else:
                                    current_cell += " " + curr["text"]
                            row_cells.append(current_cell)
                            grid.append(row_cells)

                        ocr_grid = grid
                    else:
                        warning_msg = f"OCR found only {len(words)} words — falling back to image embed."
                except Exception as ocr_ex:
                    log.warning(f"image-to-excel OCR failed: {ocr_ex}")
                    warning_msg = "OCR failed — falling back to image embed."
            else:
                warning_msg = "pytesseract not available — falling back to image embed."

            wb = Workbook()
            ws = wb.active

            if ocr_grid:
                ws.title = "OCR_Table"
                for r_idx, row_cells in enumerate(ocr_grid):
                    for c_idx, cell_val in enumerate(row_cells):
                        cell = ws.cell(row=r_idx + 1, column=c_idx + 1, value=cell_val)
                        if r_idx == 0:
                            cell.font = Font(bold=True)
                msg = f"OCR extracted {len(ocr_grid)} rows × {max(len(r) for r in ocr_grid)} columns"
            else:
                # Fallback: embed image
                ws.title = "Image"
                tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                img.save(tmp.name, format="PNG")
                tmp.close()
                xl_img = XlImage(tmp.name)
                xl_img.anchor = "B2"
                ws.add_image(xl_img)
                os.unlink(tmp.name)
                msg = "Image embedded in Excel workbook"

            filename = generate_output_filename(f.filename, "to_excel")
            filename = re.sub(r'\.(jpg|jpeg|png|gif|bmp|tiff|webp)$', '.xlsx',
                               filename, flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            wb.save(out)

            if warning_msg:
                msg += f" (Warning: {warning_msg})"

        return ok(msg, out)
    except Exception:
        log.exception("image_to_excel")
        return err("Image to Excel failed", 500)


# ═════════════════════════════════════════════════════════════════
# WORD TOOLS
# ═════════════════════════════════════════════════════════════════
@app.route("/api/word-to-jpg", methods=["POST"])
@rate_limited()
def word_to_jpg():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            pdf_path = libre(path, "pdf", temp=True)
            if not pdf_path:
                return err("LibreOffice conversion failed. Ensure LibreOffice is installed.", 500)
            doc = fitz.open(pdf_path)
            try:
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for i, page in enumerate(doc):
                        pix = page.get_pixmap(dpi=150)
                        zf.writestr(f"page_{i+1:04d}.jpg", pix.tobytes("jpeg"))
            finally:
                doc.close()
            try:
                os.remove(pdf_path)
            except Exception:
                pass
            filename = generate_output_filename(f.filename, "to_jpg")
            filename = re.sub(r'\.(doc|docx)$', '.zip', filename, flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(out, "wb") as fh:
                fh.write(buf.getvalue())
        return ok("Word converted to JPG images", out)
    except Exception:
        log.exception("word_to_jpg")
        return err("Word to JPG failed", 500)


@app.route("/api/word-to-png", methods=["POST"])
@rate_limited()
def word_to_png():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            pdf_path = libre(path, "pdf", temp=True)
            if not pdf_path:
                return err("LibreOffice conversion failed. Ensure LibreOffice is installed.", 500)
            doc = fitz.open(pdf_path)
            try:
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for i, page in enumerate(doc):
                        pix = page.get_pixmap(dpi=150)
                        zf.writestr(f"page_{i+1:04d}.png", pix.tobytes("png"))
            finally:
                doc.close()
            try:
                os.remove(pdf_path)
            except Exception:
                pass
            filename = generate_output_filename(f.filename, "to_png")
            filename = re.sub(r'\.(doc|docx)$', '.zip', filename, flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(out, "wb") as fh:
                fh.write(buf.getvalue())
        return ok("Word converted to PNG images", out)
    except Exception:
        log.exception("word_to_png")
        return err("Word to PNG failed", 500)


@app.route("/api/word-to-txt", methods=["POST"])
@rate_limited()
def word_to_txt():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            if DOCX_AVAILABLE and path.endswith(".docx"):
                doc = DocxDocument(path)
                text = "\n".join(p.text for p in doc.paragraphs)
                filename = generate_output_filename(f.filename, "to_txt")
                filename = re.sub(r'\.(doc|docx)$', '.txt', filename, flags=re.IGNORECASE)
                out = os.path.join(Config.OUTPUT_FOLDER, filename)
                with open(out, "w", encoding="utf-8") as fh:
                    fh.write(text)
            else:
                filename = generate_output_filename(f.filename, "to_txt")
                filename = re.sub(r'\.(doc|docx)$', '.txt', filename, flags=re.IGNORECASE)
                out = libre(path, "txt", output_filename=filename)
                if not out:
                    return err("LibreOffice conversion failed. Ensure LibreOffice is installed.", 500)
        return ok("Word converted to TXT", out)
    except Exception:
        log.exception("word_to_txt")
        return err("Word to TXT failed", 500)


@app.route("/api/word-to-excel", methods=["POST"])
@rate_limited()
def word_to_excel():
    """
    V7 UPGRADE: Uses python-docx direct extraction instead of LibreOffice.
    - Extracts each table to a separate Excel sheet (Table_1, Table_2, ...)
    - Creates Document_Text sheet with all paragraphs as fallback
    - .doc files only: falls back to LibreOffice
    """
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e:
        return err(e)

    try:
        with temp_upload(f) as path:
            filename = generate_output_filename(f.filename, "to_excel")
            filename = re.sub(r'\.(doc|docx)$', '.xlsx', filename, flags=re.IGNORECASE)

            # .doc files: LibreOffice fallback only
            if path.endswith(".doc"):
                out = libre(path, "xlsx", output_filename=filename)
                if not out:
                    return err(
                        "Word to Excel for .doc files requires LibreOffice. "
                        "For better results, convert to .docx first.", 500
                    )
                return ok("Word (.doc) converted to Excel via LibreOffice", out)

            if not DOCX_AVAILABLE:
                return err("Word to Excel requires python-docx. Install: pip install python-docx", 501)
            if not OPENPYXL_AVAILABLE:
                return err("Word to Excel requires openpyxl. Install: pip install openpyxl", 501)

            doc = DocxDocument(path)
            wb = Workbook()
            # Remove default sheet
            default_sheet = wb.active
            wb.remove(default_sheet)

            table_count = len(doc.tables)

            # Write each table to its own sheet
            for t_idx, table in enumerate(doc.tables):
                ws = wb.create_sheet(title=f"Table_{t_idx + 1}")
                for r_idx, row in enumerate(table.rows):
                    for c_idx, cell in enumerate(row.cells):
                        cell_obj = ws.cell(row=r_idx + 1, column=c_idx + 1, value=cell.text)
                        if r_idx == 0:
                            cell_obj.font = Font(bold=True)

            # Always create Document_Text sheet with all non-empty paragraphs
            ws_text = wb.create_sheet(title="Document_Text")
            ws_text.append(["Line", "Style", "Text"])
            ws_text["A1"].font = Font(bold=True)
            ws_text["B1"].font = Font(bold=True)
            ws_text["C1"].font = Font(bold=True)
            para_row = 2
            for p in doc.paragraphs:
                if p.text.strip():
                    ws_text.cell(row=para_row, column=1, value=para_row - 1)
                    ws_text.cell(row=para_row, column=2, value=p.style.name)
                    ws_text.cell(row=para_row, column=3, value=p.text)
                    para_row += 1

            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            wb.save(out)

        return ok(
            f"Word converted to Excel — {table_count} table(s) extracted",
            out,
            tables_found=table_count
        )
    except Exception:
        log.exception("word_to_excel")
        return err("Word to Excel failed", 500)


@app.route("/api/word-to-ppt", methods=["POST"])
@rate_limited()
def word_to_ppt():
    """
    V7 UPGRADE: Uses python-docx + python-pptx direct conversion.
    - Heading 1 → new slide with title
    - Heading 2 → subtitle / new bullet group
    - Normal/body → bullet points on current slide
    - .doc files: falls back to LibreOffice
    """
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e:
        return err(e)

    try:
        with temp_upload(f) as path:
            filename = generate_output_filename(f.filename, "to_ppt")
            filename = re.sub(r'\.(doc|docx)$', '.pptx', filename, flags=re.IGNORECASE)

            # .doc files: LibreOffice fallback
            if path.endswith(".doc"):
                out = libre(path, "pptx", output_filename=filename)
                if not out:
                    return err(
                        "Word to PPT for .doc files requires LibreOffice. "
                        "For better results, convert to .docx first.", 500
                    )
                return ok("Word (.doc) converted to PowerPoint via LibreOffice", out)

            if not DOCX_AVAILABLE:
                return err("Word to PPT requires python-docx. Install: pip install python-docx", 501)
            if not PPTX_AVAILABLE:
                return err("Word to PPT requires python-pptx. Install: pip install python-pptx", 501)

            doc = DocxDocument(path)
            prs = Presentation()
            prs.slide_width = PptxInches(10)
            prs.slide_height = PptxInches(7.5)

            # Layout 1 = Title and Content
            title_content_layout = prs.slide_layouts[1]
            blank_layout = prs.slide_layouts[6]

            paragraphs = [p for p in doc.paragraphs if p.text.strip()]

            if not paragraphs:
                # Empty document: create single blank slide
                prs.slides.add_slide(blank_layout)
                out = os.path.join(Config.OUTPUT_FOLDER, filename)
                prs.save(out)
                return ok("Word converted to PowerPoint (empty document)", out)

            # Check if any headings exist
            has_headings = any(p.style.name.startswith("Heading") for p in paragraphs)

            if not has_headings:
                # All paragraphs as bullets on single slide
                slide = prs.slides.add_slide(title_content_layout)
                title_shape = slide.shapes.title
                if title_shape:
                    title_shape.text = Path(f.filename).stem
                body_shape = slide.placeholders[1] if len(slide.placeholders) > 1 else None
                if body_shape:
                    tf = body_shape.text_frame
                    tf.clear()
                    for p in paragraphs:
                        para = tf.add_paragraph()
                        para.text = p.text
                        para.level = 0
            else:
                current_slide = None
                current_tf = None

                for p in paragraphs:
                    style_name = p.style.name
                    text = p.text.strip()

                    if style_name.startswith("Heading 1") or style_name == "Title":
                        # New slide
                        slide = prs.slides.add_slide(title_content_layout)
                        title_shape = slide.shapes.title
                        if title_shape:
                            title_shape.text = text
                        current_slide = slide
                        current_tf = None
                        if len(slide.placeholders) > 1:
                            body_ph = slide.placeholders[1]
                            current_tf = body_ph.text_frame
                            current_tf.clear()

                    elif style_name.startswith("Heading 2"):
                        if current_tf is None and current_slide is None:
                            slide = prs.slides.add_slide(title_content_layout)
                            current_slide = slide
                            if len(slide.placeholders) > 1:
                                current_tf = slide.placeholders[1].text_frame
                                current_tf.clear()
                        if current_tf:
                            para = current_tf.add_paragraph()
                            para.text = text
                            para.level = 0
                            run = para.runs[0] if para.runs else None
                            if run:
                                run.font.bold = True

                    else:
                        # Body / Normal → bullet
                        if current_slide is None:
                            slide = prs.slides.add_slide(title_content_layout)
                            current_slide = slide
                            if len(slide.placeholders) > 1:
                                current_tf = slide.placeholders[1].text_frame
                                current_tf.clear()
                        if current_tf:
                            para = current_tf.add_paragraph()
                            para.text = text
                            para.level = 1

            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            prs.save(out)

        return ok("Word converted to PowerPoint", out)
    except Exception:
        log.exception("word_to_ppt")
        return err("Word to PPT failed", 500)


@app.route("/api/word-to-html", methods=["POST"])
@rate_limited()
def word_to_html():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            filename = generate_output_filename(f.filename, "to_html")
            filename = re.sub(r'\.(doc|docx)$', '.html', filename, flags=re.IGNORECASE)
            out = libre(path, "html", output_filename=filename)
            if not out:
                return err("LibreOffice conversion failed. Ensure LibreOffice is installed.", 500)
        return ok("Word converted to HTML", out)
    except Exception:
        log.exception("word_to_html")
        return err("Word to HTML failed", 500)


@app.route("/api/word-to-json", methods=["POST"])
@rate_limited()
def word_to_json():
    if not DOCX_AVAILABLE:
        return err("Word to JSON requires python-docx. Install: pip install python-docx", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            doc = DocxDocument(path)
            data = {"paragraphs": [], "tables": []}
            for p in doc.paragraphs:
                data["paragraphs"].append({"style": p.style.name, "text": p.text})
            for table in doc.tables:
                tdata = []
                for row in table.rows:
                    tdata.append([cell.text for cell in row.cells])
                data["tables"].append(tdata)
            filename = generate_output_filename(f.filename, "to_json")
            filename = re.sub(r'\.(doc|docx)$', '.json', filename, flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        return ok("Word converted to JSON", out)
    except Exception:
        log.exception("word_to_json")
        return err("Word to JSON failed", 500)


@app.route("/api/compress-word", methods=["POST"])
@rate_limited()
def compress_word():
    """
    V7 UPGRADE: Real ZIP-level image compression inside .docx.
    - Treats .docx as ZIP, finds images in word/media/
    - Resizes images > 1200px, converts to JPEG at requested quality
    - Repacks at compresslevel=9
    - Reports reduction percentage
    - .doc files: convert to .docx via LibreOffice first
    """
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e:
        return err(e)

    quality_str = request.form.get("quality", "medium").lower()
    quality_map = {"low": 50, "medium": 70, "high": 85}
    jpeg_quality = quality_map.get(quality_str, 70)

    try:
        with temp_upload(f) as path:
            orig_size = os.path.getsize(path)
            filename = generate_output_filename(f.filename, "compressed")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)

            work_path = path

            # .doc: convert to .docx first
            if path.endswith(".doc"):
                converted = libre(path, "docx", temp=True)
                if not converted:
                    return err(
                        "LibreOffice required to process .doc files. "
                        "Ensure LibreOffice is installed or convert to .docx first.", 500
                    )
                work_path = converted

            # Extract ZIP, compress images, repack
            tmp_dir = tempfile.mkdtemp()
            try:
                with zipfile.ZipFile(work_path, 'r') as zin:
                    zin.extractall(tmp_dir)

                media_dir = os.path.join(tmp_dir, "word", "media")
                images_compressed = 0

                if os.path.isdir(media_dir):
                    image_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp"}
                    for fname_img in os.listdir(media_dir):
                        img_path = os.path.join(media_dir, fname_img)
                        if os.path.splitext(fname_img)[1].lower() not in image_exts:
                            continue
                        try:
                            img = Image.open(img_path)
                            w, h = img.size
                            # Resize if larger than 1200px on any axis
                            if w > 1200 or h > 1200:
                                ratio = min(1200 / w, 1200 / h)
                                new_w = max(1, int(w * ratio))
                                new_h = max(1, int(h * ratio))
                                img = img.resize((new_w, new_h), Image.LANCZOS)
                            # Convert to RGB for JPEG
                            if img.mode in ("RGBA", "P", "LA"):
                                bg = Image.new("RGB", img.size, (255, 255, 255))
                                if img.mode == "P":
                                    img = img.convert("RGBA")
                                mask = img.split()[-1] if img.mode in ("RGBA", "LA") else None
                                bg.paste(img, mask=mask)
                                img = bg
                            elif img.mode != "RGB":
                                img = img.convert("RGB")
                            # Replace file in-place as JPEG
                            img.save(img_path, format="JPEG", quality=jpeg_quality, optimize=True)
                            images_compressed += 1
                        except Exception as img_ex:
                            log.warning(f"Could not compress image {fname_img}: {img_ex}")

                # Repack to new ZIP at level 9
                with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zout:
                    for root, dirs, files in os.walk(tmp_dir):
                        for fname_item in files:
                            abs_path = os.path.join(root, fname_item)
                            arc_name = os.path.relpath(abs_path, tmp_dir)
                            zout.write(abs_path, arc_name)

            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
                if work_path != path:
                    try:
                        os.remove(work_path)
                    except Exception:
                        pass

            new_size = os.path.getsize(out)
            reduction = round((1 - new_size / orig_size) * 100, 1) if orig_size else 0

        return ok(
            f"Word compressed — {reduction}% smaller ({images_compressed} image(s) recompressed)",
            out,
            reduction_pct=reduction,
            images_compressed=images_compressed
        )
    except Exception:
        log.exception("compress_word")
        return err("Word compression failed", 500)


@app.route("/api/unlock-word", methods=["POST"])
@rate_limited()
def unlock_word():
    if not MSOFFCRYPTO_AVAILABLE:
        return err("Unlock Word requires msoffcrypto-tool. Install: pip install msoffcrypto-tool", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e:
        return err(e)
    pw = sanitize(request.form.get("password", ""))
    if not pw:
        return err("Password required")
    try:
        with temp_upload(f) as path:
            filename = generate_output_filename(f.filename, "unlocked")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(path, "rb") as fp:
                office_file = msoffcrypto.OfficeFile(fp)
                office_file.load_key(password=pw)
                with open(out, "wb") as fout:
                    office_file.decrypt(fout)
        return ok("Word document unlocked", out)
    except Exception:
        log.exception("unlock_word")
        return err("Unlock failed — check password", 500)


@app.route("/api/protect-word", methods=["POST"])
@rate_limited()
def protect_word():
    if not MSOFFCRYPTO_AVAILABLE:
        return err("Protect Word requires msoffcrypto-tool. Install: pip install msoffcrypto-tool", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e:
        return err(e)
    pw = sanitize(request.form.get("password", ""))
    pw2 = sanitize(request.form.get("password2", ""))
    if not pw:
        return err("Password required")
    if pw != pw2:
        return err("Passwords do not match")
    try:
        with temp_upload(f) as path:
            filename = generate_output_filename(f.filename, "protected")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(path, "rb") as fp:
                office_file = msoffcrypto.OfficeFile(fp)
                office_file.encrypt(pw, out)
        return ok("Word document password protected", out)
    except Exception:
        log.exception("protect_word")
        return err("Protect Word failed", 500)


@app.route("/api/edit-word", methods=["POST"])
@rate_limited()
def edit_word():
    if not DOCX_AVAILABLE:
        return err("Edit Word requires python-docx. Install: pip install python-docx", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e:
        return err(e)
    find_text = sanitize(request.form.get("find_text", ""))
    replace_text = sanitize(request.form.get("replace_text", ""))
    if not find_text:
        return err("Find text required")
    try:
        with temp_upload(f) as path:
            doc = DocxDocument(path)
            count = 0
            for para in doc.paragraphs:
                for run in para.runs:
                    if find_text in run.text:
                        count += run.text.count(find_text)
                        run.text = run.text.replace(find_text, replace_text)
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        for para in cell.paragraphs:
                            for run in para.runs:
                                if find_text in run.text:
                                    count += run.text.count(find_text)
                                    run.text = run.text.replace(find_text, replace_text)
            filename = generate_output_filename(f.filename, "edited")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            doc.save(out)
        return ok(f"Replaced {count} occurrence(s) in Word document", out)
    except Exception:
        log.exception("edit_word")
        return err("Edit Word failed", 500)


# ═════════════════════════════════════════════════════════════════
# EXCEL TOOLS
# ═════════════════════════════════════════════════════════════════
@app.route("/api/excel-to-csv", methods=["POST"])
@rate_limited()
def excel_to_csv():
    """
    V7 UPGRADE: Multi-sheet ZIP export.
    - all_sheets=true: export every sheet as CSV, pack into ZIP
    - all_sheets=false (default): active sheet → single CSV
    - Always utf-8-sig encoding (BOM for Excel Windows compatibility)
    """
    if not OPENPYXL_AVAILABLE:
        return err("Excel to CSV requires openpyxl. Install: pip install openpyxl", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e:
        return err(e)

    sheet_name = sanitize(request.form.get("sheet", ""), 100)
    all_sheets = request.form.get("all_sheets", "false").lower() in ("true", "1", "yes")

    try:
        with temp_upload(f) as path:
            wb = load_workbook(path, data_only=True, read_only=True)

            if all_sheets:
                # Export all sheets → ZIP
                buf = io.BytesIO()
                total_rows = 0
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for sname in wb.sheetnames:
                        ws = wb[sname]
                        csv_buf = io.StringIO()
                        writer = csv.writer(csv_buf, quoting=csv.QUOTE_MINIMAL)
                        row_count = 0
                        for row in ws.iter_rows(values_only=True):
                            writer.writerow([_coerce_cell_for_csv(v) for v in row])
                            row_count += 1
                        total_rows += row_count
                        safe_name = re.sub(r'[^\w]', '_', sname)
                        # utf-8-sig: add BOM manually since StringIO → bytes
                        csv_bytes = ('\ufeff' + csv_buf.getvalue()).encode('utf-8')
                        zf.writestr(f"{safe_name}.csv", csv_bytes)

                wb.close()

                filename = generate_output_filename(f.filename, "to_csv")
                filename = re.sub(r'\.(xls|xlsx)$', '.zip', filename, flags=re.IGNORECASE)
                out = os.path.join(Config.OUTPUT_FOLDER, filename)
                with open(out, "wb") as fh:
                    fh.write(buf.getvalue())

                return ok(
                    f"All {len(wb.sheetnames)} sheet(s) exported to ZIP ({total_rows} total rows)",
                    out
                )

            else:
                # Single sheet
                if sheet_name and sheet_name in wb.sheetnames:
                    ws = wb[sheet_name]
                else:
                    ws = wb.active

                filename = generate_output_filename(f.filename, "to_csv")
                filename = re.sub(r'\.(xls|xlsx)$', '.csv', filename, flags=re.IGNORECASE)
                out = os.path.join(Config.OUTPUT_FOLDER, filename)

                row_count = 0
                with open(out, "w", newline="", encoding="utf-8-sig") as fh:
                    writer = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
                    for row in ws.iter_rows(values_only=True):
                        writer.writerow([_coerce_cell_for_csv(v) for v in row])
                        row_count += 1

                wb.close()
                return ok(f"Excel converted to CSV ({row_count} rows)", out)

    except Exception:
        log.exception("excel_to_csv")
        return err("Excel to CSV failed", 500)


@app.route("/api/excel-to-jpg", methods=["POST"])
@rate_limited()
def excel_to_jpg():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            pdf_path = libre(path, "pdf", temp=True)

            if pdf_path and os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 100:
                doc = fitz.open(pdf_path)
                try:
                    if len(doc) == 0:
                        pdf_path = None
                    else:
                        buf = io.BytesIO()
                        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                            for i, page in enumerate(doc):
                                pix = page.get_pixmap(dpi=150)
                                zf.writestr(f"sheet_{i+1:04d}.jpg", pix.tobytes("jpeg"))
                        filename = generate_output_filename(f.filename, "to_jpg")
                        filename = re.sub(r'\.(xls|xlsx)$', '.zip', filename, flags=re.IGNORECASE)
                        out = os.path.join(Config.OUTPUT_FOLDER, filename)
                        with open(out, "wb") as fh:
                            fh.write(buf.getvalue())
                        return ok("Excel sheets exported as JPG", out)
                finally:
                    doc.close()
                    try:
                        os.remove(pdf_path)
                    except Exception:
                        pass

            log.warning("excel-to-jpg: LibreOffice failed, using openpyxl fallback renderer")
            out = _excel_render_fallback(path, f.filename, "jpg")
            if not out:
                return err("Excel to JPG failed — LibreOffice unavailable and fallback failed", 500)
            return ok("Excel sheets exported as JPG (fallback renderer)", out)

    except Exception:
        log.exception("excel_to_jpg")
        return err("Excel to JPG failed", 500)


@app.route("/api/excel-to-png", methods=["POST"])
@rate_limited()
def excel_to_png():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            pdf_path = libre(path, "pdf", temp=True)

            if pdf_path and os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 100:
                doc = fitz.open(pdf_path)
                try:
                    if len(doc) > 0:
                        buf = io.BytesIO()
                        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                            for i, page in enumerate(doc):
                                pix = page.get_pixmap(dpi=150)
                                zf.writestr(f"sheet_{i+1:04d}.png", pix.tobytes("png"))
                        filename = generate_output_filename(f.filename, "to_png")
                        filename = re.sub(r'\.(xls|xlsx)$', '.zip', filename, flags=re.IGNORECASE)
                        out = os.path.join(Config.OUTPUT_FOLDER, filename)
                        with open(out, "wb") as fh:
                            fh.write(buf.getvalue())
                        return ok("Excel sheets exported as PNG", out)
                finally:
                    doc.close()
                    try:
                        os.remove(pdf_path)
                    except Exception:
                        pass

            log.warning("excel-to-png: LibreOffice failed, using openpyxl fallback renderer")
            out = _excel_render_fallback(path, f.filename, "png")
            if not out:
                return err("Excel to PNG failed — LibreOffice unavailable and fallback failed", 500)
            return ok("Excel sheets exported as PNG (fallback renderer)", out)

    except Exception:
        log.exception("excel_to_png")
        return err("Excel to PNG failed", 500)


def _excel_render_fallback(xlsx_path: str, original_filename: str, fmt: str) -> Optional[str]:
    if not OPENPYXL_AVAILABLE:
        return None
    try:
        wb = load_workbook(xlsx_path, data_only=True, read_only=True)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                rows = list(ws.iter_rows(values_only=True, max_row=200, max_col=30))
                if not rows:
                    continue

                cell_w, cell_h, pad = 120, 22, 8
                n_cols = max(len(r) for r in rows)
                n_rows = len(rows)
                img_w = pad * 2 + n_cols * cell_w
                img_h = pad * 2 + n_rows * cell_h + 30

                img = Image.new("RGB", (img_w, img_h), (255, 255, 255))

                try:
                    from PIL import ImageDraw
                    draw = ImageDraw.Draw(img)
                    draw.text((pad, 4), f"Sheet: {sheet_name}", fill=(0, 0, 0))
                    for r_idx, row in enumerate(rows):
                        y = pad + 30 + r_idx * cell_h
                        for c_idx in range(n_cols):
                            x = pad + c_idx * cell_w
                            draw.rectangle([x, y, x + cell_w - 1, y + cell_h - 1],
                                           outline=(200, 200, 200))
                            if r_idx == 0:
                                draw.rectangle([x + 1, y + 1, x + cell_w - 2, y + cell_h - 2],
                                               fill=(230, 230, 240))
                            val = row[c_idx] if c_idx < len(row) else None
                            cell_text = _coerce_cell_for_csv(val)[:18]
                            draw.text((x + 3, y + 4), cell_text, fill=(0, 0, 0))
                except Exception as draw_ex:
                    log.warning(f"Fallback draw error: {draw_ex}")

                ib = io.BytesIO()
                img.save(ib, format=fmt.upper() if fmt != "jpg" else "JPEG",
                         quality=85 if fmt == "jpg" else None)
                sheet_safe = re.sub(r'[^\w]', '_', sheet_name)
                zf.writestr(f"{sheet_safe}.{fmt}", ib.getvalue())

        wb.close()

        filename = generate_output_filename(original_filename, f"to_{fmt}")
        filename = re.sub(r'\.(xls|xlsx)$', '.zip', filename, flags=re.IGNORECASE)
        out = os.path.join(Config.OUTPUT_FOLDER, filename)
        with open(out, "wb") as fh:
            fh.write(buf.getvalue())
        return out

    except Exception as ex:
        log.error(f"Excel fallback render failed: {ex}")
        return None


@app.route("/api/excel-to-word", methods=["POST"])
@rate_limited()
def excel_to_word():
    """
    V7 UPGRADE: Uses openpyxl + python-docx direct build instead of LibreOffice.
    - Each sheet → Heading 1 + Word table (style: Light Grid Accent 1)
    - First data row → bold header row
    - Limits to 200 rows per sheet (adds note if truncated)
    - Falls back to LibreOffice only if python-docx or openpyxl unavailable
    """
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e:
        return err(e)

    if not OPENPYXL_AVAILABLE or not DOCX_AVAILABLE:
        # Fallback to LibreOffice
        try:
            with temp_upload(f) as path:
                filename = generate_output_filename(f.filename, "to_word")
                filename = re.sub(r'\.(xls|xlsx)$', '.docx', filename, flags=re.IGNORECASE)
                out = libre(path, "docx", output_filename=filename)
                if not out:
                    return err(
                        "Excel to Word requires openpyxl + python-docx (or LibreOffice as fallback). "
                        "Please install: pip install openpyxl python-docx", 500
                    )
            return ok("Excel converted to Word (LibreOffice)", out)
        except Exception:
            log.exception("excel_to_word_libre_fallback")
            return err("Excel to Word failed", 500)

    try:
        with temp_upload(f) as path:
            wb = load_workbook(path, data_only=True)
            doc = DocxDocument()

            ROW_LIMIT = 200

            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                doc.add_heading(sheet_name, level=1)

                all_rows = list(ws.iter_rows(values_only=True, max_row=ROW_LIMIT + 1))
                truncated = len(all_rows) > ROW_LIMIT
                rows_to_write = all_rows[:ROW_LIMIT]

                if not rows_to_write:
                    doc.add_paragraph("(empty sheet)")
                    continue

                n_cols = max((len(r) for r in rows_to_write), default=1)

                try:
                    table = doc.add_table(rows=len(rows_to_write), cols=n_cols)
                    table.style = 'Light Grid Accent 1'
                except Exception:
                    table = doc.add_table(rows=len(rows_to_write), cols=n_cols)

                for r_idx, row_data in enumerate(rows_to_write):
                    for c_idx in range(n_cols):
                        val = row_data[c_idx] if c_idx < len(row_data) else None
                        cell_text = _coerce_cell_for_csv(val)
                        cell = table.cell(r_idx, c_idx)
                        cell.text = cell_text
                        if r_idx == 0:
                            # Bold header row
                            for para in cell.paragraphs:
                                for run in para.runs:
                                    run.bold = True

                if truncated:
                    doc.add_paragraph(
                        f"(Note: Sheet truncated to {ROW_LIMIT} rows. "
                        f"Original sheet may have more data.)"
                    )
                doc.add_paragraph()  # spacing between sheets

            wb.close()

            filename = generate_output_filename(f.filename, "to_word")
            filename = re.sub(r'\.(xls|xlsx)$', '.docx', filename, flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            doc.save(out)

        return ok(f"Excel converted to Word ({len(wb.sheetnames)} sheet(s))", out)
    except Exception:
        log.exception("excel_to_word")
        return err("Excel to Word failed", 500)


@app.route("/api/excel-to-ppt", methods=["POST"])
@rate_limited()
def excel_to_ppt():
    """
    V7.1 UPGRADE: Direct openpyxl → python-pptx conversion.
    Each sheet becomes a slide with a formatted table.
    """
    if not OPENPYXL_AVAILABLE:
        return err("Excel to PPT requires openpyxl. Install: pip install openpyxl", 501)
    if not PPTX_AVAILABLE:
        return err("Excel to PPT requires python-pptx. Install: pip install python-pptx", 501)
    
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e:
        return err(e)
    
    try:
        with temp_upload(f) as path:
            wb = load_workbook(path, data_only=True)
            prs = Presentation()
            prs.slide_width = PptxInches(10)
            prs.slide_height = PptxInches(7.5)
            
            # Use Title and Content layout
            title_content_layout = prs.slide_layouts[1]
            
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                
                # Get all non-empty rows
                rows = []
                for row in ws.iter_rows(values_only=True):
                    if any(cell is not None for cell in row):
                        rows.append(row)
                
                if not rows:
                    continue
                
                # Create slide
                slide = prs.slides.add_slide(title_content_layout)
                
                # Set title
                if slide.shapes.title:
                    slide.shapes.title.text = sheet_name
                
                # Determine table dimensions
                max_cols = max(len(row) for row in rows)
                max_rows = min(len(rows), 25)  # Limit to 25 rows per slide
                
                # Add table shape
                left = PptxInches(0.5)
                top = PptxInches(1.5)
                width = PptxInches(9)
                height = PptxInches(5)
                
                table_shape = slide.shapes.add_table(max_rows, max_cols, left, top, width, height)
                table = table_shape.table
                
                # Fill table
                for r_idx in range(max_rows):
                    row_data = rows[r_idx]
                    for c_idx in range(max_cols):
                        cell = table.cell(r_idx, c_idx)
                        val = row_data[c_idx] if c_idx < len(row_data) else ""
                        cell.text = _coerce_cell_for_csv(val)
                        
                        # Format header row
                        if r_idx == 0:
                            cell.text_frame.paragraphs[0].font.bold = True
                
                # Add note if truncated
                if len(rows) > max_rows:
                    note_box = slide.shapes.add_textbox(
                        PptxInches(0.5), PptxInches(6.8),
                        PptxInches(9), PptxInches(0.5)
                    )
                    note_frame = note_box.text_frame
                    note_frame.text = f"(Showing first {max_rows} rows. Sheet has {len(rows)} total rows.)"
                    note_frame.paragraphs[0].font.size = PptxPt(10)
                    note_frame.paragraphs[0].font.italic = True
            
            wb.close()
            
            filename = generate_output_filename(f.filename, "to_ppt")
            filename = re.sub(r'\.(xls|xlsx)$', '.pptx', filename, flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            prs.save(out)
            
        return ok(f"Excel converted to PowerPoint ({len(wb.sheetnames)} slide(s))", out)
    except Exception:
        log.exception("excel_to_ppt")
        # Fallback to LibreOffice
        try:
            with temp_upload(f) as path:
                filename = generate_output_filename(f.filename, "to_ppt")
                filename = re.sub(r'\.(xls|xlsx)$', '.pptx', filename, flags=re.IGNORECASE)
                out = libre(path, "pptx", output_filename=filename)
                if out:
                    return ok("Excel converted to PowerPoint (LibreOffice fallback)", out)
        except Exception:
            pass
        return err("Excel to PPT failed", 500)


@app.route("/api/excel-to-html", methods=["POST"])
@rate_limited()
def excel_to_html():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            filename = generate_output_filename(f.filename, "to_html")
            filename = re.sub(r'\.(xls|xlsx)$', '.html', filename, flags=re.IGNORECASE)
            out = libre(path, "html", output_filename=filename)
            if not out:
                return err("LibreOffice conversion failed. Ensure LibreOffice is installed.", 500)
        return ok("Excel converted to HTML", out)
    except Exception:
        log.exception("excel_to_html")
        return err("Excel to HTML failed", 500)


@app.route("/api/excel-to-json", methods=["POST"])
@rate_limited()
def excel_to_json():
    if not OPENPYXL_AVAILABLE:
        return err("Excel to JSON requires openpyxl. Install: pip install openpyxl", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e:
        return err(e)

    use_headers = request.form.get("header", "true").lower() in ("true", "1", "yes")

    try:
        with temp_upload(f) as path:
            wb = load_workbook(path, data_only=True, read_only=True)
            data = {}

            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                all_rows = list(ws.iter_rows(values_only=True))

                if not all_rows:
                    data[sheet_name] = []
                    continue

                if use_headers and len(all_rows) > 0:
                    headers = [str(h) if h is not None else f"col_{i}" for i, h in enumerate(all_rows[0])]
                    sheet_data = []
                    for row in all_rows[1:]:
                        row_obj = {}
                        for col_idx, header in enumerate(headers):
                            val = row[col_idx] if col_idx < len(row) else None
                            row_obj[header] = _coerce_cell_value(val)
                        sheet_data.append(row_obj)
                    data[sheet_name] = sheet_data
                else:
                    sheet_data = []
                    for row in all_rows:
                        sheet_data.append([_coerce_cell_value(v) for v in row])
                    data[sheet_name] = sheet_data

            wb.close()

            filename = generate_output_filename(f.filename, "to_json")
            filename = re.sub(r'\.(xls|xlsx)$', '.json', filename, flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, filename)

            with open(out, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2, default=str)

        return ok(f"Excel converted to JSON ({len(data)} sheet(s), types preserved)", out)
    except Exception:
        log.exception("excel_to_json")
        return err("Excel to JSON failed", 500)


@app.route("/api/compress-excel", methods=["POST"])
@rate_limited()
def compress_excel():
    if not OPENPYXL_AVAILABLE:
        return err("Compress Excel requires openpyxl. Install: pip install openpyxl", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e:
        return err(e)

    try:
        with temp_upload(f) as path:
            orig = os.path.getsize(path)
            wb = load_workbook(path, data_only=True)

            for ws in wb.worksheets:
                max_data_row = 0
                max_data_col = 0
                for row in ws.iter_rows():
                    for cell in row:
                        if cell.value is not None:
                            max_data_row = max(max_data_row, cell.row)
                            max_data_col = max(max_data_col, cell.column)

                if max_data_row > 0 and ws.max_row > max_data_row:
                    rows_to_delete = ws.max_row - max_data_row
                    if rows_to_delete > 0:
                        try:
                            ws.delete_rows(max_data_row + 1, rows_to_delete)
                        except Exception:
                            pass

                if max_data_col > 0 and ws.max_column > max_data_col:
                    cols_to_delete = ws.max_column - max_data_col
                    if cols_to_delete > 0:
                        try:
                            ws.delete_cols(max_data_col + 1, cols_to_delete)
                        except Exception:
                            pass

            try:
                for dn in list(wb.defined_names.definedName):
                    if hasattr(dn, 'attr_text') and dn.attr_text and '#REF' in str(dn.attr_text):
                        del wb.defined_names[dn.name]
            except Exception:
                pass

            tmp_out = path + "_compressed_tmp.xlsx"
            wb.save(tmp_out)
            wb.close()

            filename = generate_output_filename(f.filename, "compressed")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)

            recompressed = _recompress_xlsx(tmp_out, out)
            if not recompressed:
                shutil.copy(tmp_out, out)

            try:
                os.remove(tmp_out)
            except Exception:
                pass

            new_size = os.path.getsize(out)
            reduction = round((1 - new_size / orig) * 100, 1) if orig else 0

        return ok(f"Excel compressed — {reduction}% smaller", out, reduction_pct=reduction)
    except Exception:
        log.exception("compress_excel")
        return err("Excel compression failed", 500)


def _recompress_xlsx(input_path: str, output_path: str) -> bool:
    try:
        with zipfile.ZipFile(input_path, 'r') as zin:
            with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as zout:
                for item in zin.infolist():
                    data = zin.read(item.filename)
                    zout.writestr(item, data)
        return os.path.getsize(output_path) > 0
    except Exception as ex:
        log.warning(f"xlsx recompress failed: {ex}")
        return False


@app.route("/api/unlock-excel", methods=["POST"])
@rate_limited()
def unlock_excel():
    if not MSOFFCRYPTO_AVAILABLE:
        return err("Unlock Excel requires msoffcrypto-tool. Install: pip install msoffcrypto-tool", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e:
        return err(e)
    pw = sanitize(request.form.get("password", ""))
    if not pw:
        return err("Password required")
    try:
        with temp_upload(f) as path:
            filename = generate_output_filename(f.filename, "unlocked")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(path, "rb") as fp:
                office_file = msoffcrypto.OfficeFile(fp)
                office_file.load_key(password=pw)
                with open(out, "wb") as fout:
                    office_file.decrypt(fout)
        return ok("Excel workbook unlocked", out)
    except Exception:
        log.exception("unlock_excel")
        return err("Unlock failed — check password", 500)


@app.route("/api/protect-excel", methods=["POST"])
@rate_limited()
def protect_excel():
    if not MSOFFCRYPTO_AVAILABLE:
        return err("Protect Excel requires msoffcrypto-tool. Install: pip install msoffcrypto-tool", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e:
        return err(e)
    pw = sanitize(request.form.get("password", ""))
    pw2 = sanitize(request.form.get("password2", ""))
    if not pw:
        return err("Password required")
    if pw != pw2:
        return err("Passwords do not match")
    try:
        with temp_upload(f) as path:
            filename = generate_output_filename(f.filename, "protected")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(path, "rb") as fp:
                office_file = msoffcrypto.OfficeFile(fp)
                office_file.encrypt(pw, out)
        return ok("Excel workbook password protected", out)
    except Exception:
        log.exception("protect_excel")
        return err("Protect Excel failed", 500)


@app.route("/api/repair-excel", methods=["POST"])
@rate_limited()
def repair_excel():
    if not OPENPYXL_AVAILABLE:
        return err("Repair Excel requires openpyxl. Install: pip install openpyxl", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            filename = generate_output_filename(f.filename, "repaired")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)

            try:
                wb = load_workbook(path, data_only=False, read_only=False)
                wb.save(out)
                wb.close()
                if os.path.exists(out) and os.path.getsize(out) > 0:
                    return ok("Excel workbook repaired (openpyxl)", out)
            except Exception as ex1:
                log.warning(f"openpyxl repair pass 1 failed: {ex1}")

            lo_out = libre(path, "xlsx", output_filename=filename)
            if lo_out and os.path.exists(lo_out) and os.path.getsize(lo_out) > 0:
                return ok("Excel workbook repaired (LibreOffice)", lo_out)

            return err("Could not repair Excel file — it may be severely corrupted", 500)

    except Exception:
        log.exception("repair_excel")
        return err("Excel repair failed", 500)


# ─────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
