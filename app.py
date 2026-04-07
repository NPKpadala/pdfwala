"""
PDFWala - Complete Production Backend V5.1
All bugs fixed. Production ready.
"""

import os, io, uuid, zipfile, logging, time, threading, subprocess
import tempfile, shutil, re, csv, json
from contextlib import contextmanager
from functools import wraps
from datetime import datetime
from typing import Optional
from pathlib import Path

from flask import Flask, request, jsonify, send_file, send_from_directory, render_template, abort, g
from flask_cors import CORS
from werkzeug.utils import secure_filename

import fitz  # PyMuPDF
from PIL import Image, ImageChops
from PyPDF2 import PdfReader, PdfWriter, PdfMerger
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import letter, A4

try:
    from docx import Document as DocxDocument
    from docx.shared import Inches, Pt
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

try:
    from openpyxl import load_workbook, Workbook
    from openpyxl.drawing.image import Image as XlImage
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
# CONFIG
# ─────────────────────────────────────────────────────────────────
class Config:
    BASE_DIR        = os.environ.get("BASE_DIR", "/home/opc/pdfwala")
    UPLOAD_FOLDER   = os.environ.get("UPLOAD_FOLDER", os.path.join(BASE_DIR, "uploads"))
    OUTPUT_FOLDER   = os.environ.get("OUTPUT_FOLDER", os.path.join(BASE_DIR, "outputs"))
    STATIC_FOLDER   = os.environ.get("STATIC_FOLDER", os.path.join(BASE_DIR, "static"))
    MAX_FILE_SIZE   = int(os.environ.get("MAX_FILE_SIZE", 200 * 1024 * 1024))
    MAX_FILES_MERGE = int(os.environ.get("MAX_FILES_MERGE", 30))
    FILE_TTL_SEC    = int(os.environ.get("FILE_TTL_SEC", 3600))
    RATE_LIMIT      = int(os.environ.get("RATE_LIMIT", 30))
    SECRET_KEY      = os.environ.get("SECRET_KEY", uuid.uuid4().hex)
    LIBREOFFICE     = os.environ.get("LIBREOFFICE_PATH", "soffice")
    GHOSTSCRIPT     = os.environ.get("GHOSTSCRIPT_PATH", "gs")

    ALLOWED_PDF   = {"pdf"}
    ALLOWED_IMAGE = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tiff"}
    ALLOWED_DOC   = {"doc", "docx"}
    ALLOWED_XLS   = {"xls", "xlsx"}
    ALLOWED_PPT   = {"ppt", "pptx"}
    ALLOWED_HTML  = {"html", "htm"}
    ALLOWED_WEBP  = {"webp"}
    ALLOWED_PNG   = {"png"}
    ALLOWED_JPG   = {"jpg", "jpeg"}

    OLE_MAGIC = b"\xd0\xcf\x11\xe0"

for _d in [Config.UPLOAD_FOLDER, Config.OUTPUT_FOLDER]:
    os.makedirs(_d, exist_ok=True)

_APP_START = time.time()

# ─────────────────────────────────────────────────────────────────
# APP INIT
# ─────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=Config.STATIC_FOLDER, static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = Config.MAX_FILE_SIZE
app.secret_key = Config.SECRET_KEY
CORS(app, resources={r"/api/*": {"origins": os.environ.get("ALLOWED_ORIGINS", "*")}})

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)
log = logging.getLogger("pdfwala")


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
# FILE VALIDATION — FIXED
# ─────────────────────────────────────────────────────────────────
def _detect_mime(file_obj) -> Optional[str]:
    header = file_obj.read(512)
    file_obj.seek(0)
    if header[:4] == Config.OLE_MAGIC:
        return "application/msoffice"
    if header[:4] == b"PK\x03\x04":
        chunk = file_obj.read(2048)
        file_obj.seek(0)
        if b"word/" in chunk:  return "application/msword"
        if b"xl/"   in chunk:  return "application/vnd.ms-excel"
        if b"ppt/"  in chunk:  return "application/vnd.ms-powerpoint"
        return "application/zip"
    if header[:4] == b"%PDF":               return "application/pdf"
    if header[:3] == b"\xff\xd8\xff":       return "image/jpeg"
    if header[:8] == b"\x89PNG\r\n\x1a\n":  return "image/png"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP": return "image/webp"
    if header[:6] in (b"GIF87a", b"GIF89a"): return "image/gif"
    if header[:2] == b"BM":                 return "image/bmp"
    if header[:4] in (b"II*\x00", b"MM\x00*"): return "image/tiff"
    if b"<!DOCTYPE" in header or b"<html" in header.lower(): return "text/html"
    return None


def validate_file(file, allowed_ext: set) -> Optional[str]:
    """Returns error string or None if valid."""
    if not file or not file.filename:
        return "No file provided"

    ext = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""

    if ext not in allowed_ext:
        return f"Invalid file type. Allowed: {', '.join(sorted(allowed_ext))}"

    # Size check
    file.seek(0, 2)
    size = file.tell()
    file.seek(0)

    if size == 0:
        return "File is empty"

    if size > Config.MAX_FILE_SIZE:
        return f"File too large (max {Config.MAX_FILE_SIZE // 1048576} MB)"

    # HTML files — skip magic byte check (plain text)
    if ext in {"html", "htm"}:
        return None

    mime = _detect_mime(file)

    # Allow OLE (old .doc/.xls) and ZIP-based (docx/xlsx/pptx)
    if mime in ("application/msoffice", "application/msword",
                "application/vnd.ms-excel", "application/vnd.ms-powerpoint",
                "application/zip"):
        return None

    mime_ext_map = {
        "application/pdf": {"pdf"},
        "image/jpeg":      {"jpg", "jpeg"},
        "image/png":       {"png"},
        "image/webp":      {"webp"},
        "image/gif":       {"gif"},
        "image/bmp":       {"bmp"},
        "image/tiff":      {"tiff"},
        "text/html":       {"html", "htm"},
    }

    if mime and ext not in mime_ext_map.get(mime, {ext}):
        return f"File content does not match extension .{ext}"

    return None


# ─────────────────────────────────────────────────────────────────
# INTELLIGENT FILENAME GENERATOR
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


# ─────────────────────────────────────────────────────────────────
# CONTEXT MANAGERS
# ─────────────────────────────────────────────────────────────────
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
        size  = os.path.getsize(path)
        payload.update({
            "download_url": f"/download/{fname}",
            "filename":     fname,
            "size_human":   f"{size/1048576:.2f} MB" if size > 1048576 else f"{size/1024:.1f} KB",
            "expires_in":   f"{Config.FILE_TTL_SEC // 60} minutes"
        })
    return jsonify(payload)


def sanitize(text: str, maxlen: int = 500) -> str:
    return (text or "").strip()[:maxlen]


# ─────────────────────────────────────────────────────────────────
# LIBREOFFICE HELPER — FIXED temp file leak
# ─────────────────────────────────────────────────────────────────
def libre(input_path: str, fmt: str, output_filename: str = None, temp: bool = False) -> Optional[str]:
    """
    Run LibreOffice headless conversion.
    temp=True  → saves to system temp dir (caller must clean up)
    output_filename → saves to OUTPUT_FOLDER with given name
    default    → UUID file in OUTPUT_FOLDER
    """
    out_dir = tempfile.mkdtemp()
    try:
        result = subprocess.run(
            [Config.LIBREOFFICE, "--headless", "--convert-to", fmt, "--outdir", out_dir, input_path],
            capture_output=True,
            timeout=120
        )
        if result.returncode != 0:
            log.error(f"LibreOffice failed: {result.stderr.decode()[:300]}")
            return None

        base = os.path.splitext(os.path.basename(input_path))[0]
        converted = os.path.join(out_dir, f"{base}.{fmt}")
        if not os.path.exists(converted):
            # Try finding any output file with the right extension
            matches = list(Path(out_dir).glob(f"*.{fmt}"))
            if not matches:
                return None
            converted = str(matches[0])

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
# WATERMARK HELPER — ADDED (was missing)
# ─────────────────────────────────────────────────────────────────
def _make_watermark(text: str, opacity: float, color_hex: str,
                    page_width: float, page_height: float) -> bytes:
    """Generate a watermark PDF page using ReportLab."""
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(page_width, page_height))

    try:
        r = int(color_hex[0:2], 16) / 255
        g_val = int(color_hex[2:4], 16) / 255
        b = int(color_hex[4:6], 16) / 255
    except Exception:
        r, g_val, b = 0.5, 0.5, 0.5

    c.setFillColorRGB(r, g_val, b, alpha=max(0.05, min(opacity, 0.95)))
    c.setFont("Helvetica-Bold", min(page_width, page_height) * 0.08)

    c.saveState()
    c.translate(page_width / 2, page_height / 2)
    c.rotate(45)
    c.drawCentredString(0, 0, text)
    # Second line slightly offset for density
    c.drawCentredString(0, min(page_width, page_height) * 0.12, text)
    c.drawCentredString(0, -min(page_width, page_height) * 0.12, text)
    c.restoreState()

    c.save()
    buf.seek(0)
    return buf.read()


# ─────────────────────────────────────────────────────────────────
# PAGE NUMBER HELPER — ADDED (was missing)
# ─────────────────────────────────────────────────────────────────
def _make_page_num(label: str, position: str,
                   page_width: float, page_height: float) -> bytes:
    """Generate a page number overlay PDF using ReportLab."""
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(page_width, page_height))
    c.setFont("Helvetica", 10)
    c.setFillColorRGB(0.2, 0.2, 0.2)

    if position == "top":
        y = page_height - 20
    else:
        y = 15

    c.drawCentredString(page_width / 2, y, label)
    c.save()
    buf.seek(0)
    return buf.read()


# ─────────────────────────────────────────────────────────────────
# BACKGROUND CLEANUP WORKER
# ─────────────────────────────────────────────────────────────────
def _cleanup_worker():
    """Delete output files older than FILE_TTL_SEC. Runs every 10 minutes."""
    while True:
        try:
            now = time.time()
            for fname in os.listdir(Config.OUTPUT_FOLDER):
                fpath = os.path.join(Config.OUTPUT_FOLDER, fname)
                try:
                    if os.path.isfile(fpath):
                        age = now - os.path.getmtime(fpath)
                        if age > Config.FILE_TTL_SEC:
                            os.remove(fpath)
                            log.info(f"Cleaned up expired file: {fname}")
                except Exception as e:
                    log.warning(f"Cleanup error for {fname}: {e}")
        except Exception as e:
            log.error(f"Cleanup worker error: {e}")
        time.sleep(600)  # every 10 minutes


_cleanup_thread = threading.Thread(target=_cleanup_worker, daemon=True)
_cleanup_thread.start()


# ─────────────────────────────────────────────────────────────────
# PAGE RANGE PARSER
# ─────────────────────────────────────────────────────────────────
def _parse_pages(spec: str, total: int) -> list:
    """Parse '1,3,5-8' → [0, 2, 4, 5, 6, 7] (0-indexed)."""
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
# STATIC + HEALTH + DOWNLOAD
# ─────────────────────────────────────────────────────────────────
@app.route("/")
def home():
    return send_from_directory(Config.STATIC_FOLDER, "index.html")

@app.route("/pdfwala/merge-pdf")
def merge_pdf_page():
    return render_template("merge.html")

@app.route("/pdfwala/compress-pdf")
def compress_pdf_page():
    return build_seo_landing("compress-pdf")

@app.route("/pdfwala/jpg-to-pdf")
def jpg_to_pdf_page():
    return seo_landing("jpg-to-pdf")

@app.route("/pdfwala/pdf-to-word")
def pdf_to_word_page():
    return seo_landing("pdf-to-word")

@app.route("/api/health")
def health():
    lo = False
    gs = False
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
    return jsonify({
        "success": True, "status": "ok", "version": "5.1.0",
        "uptime_seconds": round(time.time() - _APP_START, 1),
        "tools_available": {
            "libreoffice": lo,
            "tesseract": TESSERACT_AVAILABLE,
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
                    ".docx", ".xlsx", ".pptx", ".txt", ".json", ".html", ".csv")
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
    mode   = request.form.get("mode", "all")
    ranges = request.form.get("ranges", "")
    try:
        with temp_upload(f) as path:
            reader  = PdfReader(path)
            total   = len(reader.pages)
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
    order  = request.form.get("order", "").strip()
    if not order:
        return err("Order/pages parameter required")
    try:
        with temp_upload(f) as path:
            reader    = PdfReader(path)
            total     = len(reader.pages)
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
            total  = len(reader.pages)
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
            reader  = PdfReader(path)
            total   = len(reader.pages)
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
    q_val = {"low": 30, "medium": 55, "high": 80}.get(quality, 55)
    try:
        with temp_upload(f) as path:
            orig = os.path.getsize(path)
            doc  = fitz.open(path)
            for page in doc:
                for img in page.get_images(full=True):
                    xref = img[0]
                    try:
                        base = doc.extract_image(xref)
                        pil  = Image.open(io.BytesIO(base["image"])).convert("RGB")
                        buf  = io.BytesIO()
                        pil.save(buf, format="JPEG", quality=q_val, optimize=True)
                        doc.update_stream(xref, buf.getvalue())
                    except Exception:
                        pass
            filename = generate_output_filename(f.filename, "compressed")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            doc.save(out, deflate=True, garbage=4, clean=True)
            doc.close()
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
            doc = fitz.open(path)
            filename = generate_output_filename(f.filename, "repaired")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            doc.save(out, garbage=4, deflate=True, clean=True)
            doc.close()
        return ok("PDF repaired successfully", out)
    except Exception:
        log.exception("repair_pdf")
        return err("Repair failed", 500)


@app.route("/api/ocr-pdf", methods=["POST"])
@rate_limited()
def ocr_pdf():
    if not TESSERACT_AVAILABLE:
        return err("OCR requires pytesseract. Install: pip install pytesseract", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    lang = sanitize(request.form.get("lang", "eng"), 10)
    try:
        with temp_upload(f) as path:
            doc     = fitz.open(path)
            new_doc = fitz.open()
            for page in doc:
                pix     = page.get_pixmap(dpi=300)
                tmp_img = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                tmp_img.write(pix.tobytes("png"))
                tmp_img.close()
                try:
                    text = pytesseract.image_to_string(tmp_img.name, lang=lang)
                except Exception:
                    text = ""
                new_page = new_doc.new_page(width=page.rect.width, height=page.rect.height)
                new_page.insert_image(page.rect, filename=tmp_img.name)
                if text.strip():
                    new_page.insert_text((50, 50), text[:2000], fontsize=8, overlay=False)
                os.unlink(tmp_img.name)
            filename = generate_output_filename(f.filename, "ocr")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            new_doc.save(out)
            new_doc.close()
            doc.close()
        return ok("OCR completed — PDF is now text-searchable", out)
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
        return err("Angle must be an integer")
    pages = request.form.get("pages", "all").strip()
    if angle not in (90, 180, 270):
        return err("Angle must be 90, 180, or 270")
    try:
        with temp_upload(f) as path:
            reader = PdfReader(path)
            total  = len(reader.pages)
            w      = PdfWriter()
            idxs   = list(range(total)) if pages.lower() == "all" else _parse_pages(pages, total)
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
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    text    = sanitize(request.form.get("text", "CONFIDENTIAL"))
    color   = sanitize(request.form.get("color", "808080"), 10)
    opacity = float(request.form.get("opacity", "0.3"))
    try:
        with temp_upload(f) as path:
            doc = fitz.open(path)
            for page in doc:
                r       = page.rect
                wm_bytes = _make_watermark(text, opacity, color, r.width, r.height)
                wmpdf   = fitz.open("pdf", wm_bytes)
                page.show_pdf_page(fitz.Rect(0, 0, r.width, r.height), wmpdf, 0, overlay=True)
                wmpdf.close()
            filename = generate_output_filename(f.filename, "watermarked")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            doc.save(out)
            doc.close()
        return ok("Watermark added to all pages", out)
    except Exception:
        log.exception("watermark")
        return err("Watermark failed", 500)


@app.route("/api/page-numbers", methods=["POST"])
@rate_limited()
def page_numbers():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    position = request.form.get("position", "bottom")
    start    = int(request.form.get("start", "1"))
    prefix   = sanitize(request.form.get("prefix", ""), 50)
    try:
        with temp_upload(f) as path:
            doc = fitz.open(path)
            for i, page in enumerate(doc):
                r     = page.rect
                label = f"{prefix}{start + i}"
                pn    = _make_page_num(label, position, r.width, r.height)
                pnpdf = fitz.open("pdf", pn)
                page.show_pdf_page(fitz.Rect(0, 0, r.width, r.height), pnpdf, 0, overlay=True)
                pnpdf.close()
            filename = generate_output_filename(f.filename, "numbered")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            doc.save(out)
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
    left   = float(request.form.get("left",   "0"))
    right  = float(request.form.get("right",  "0"))
    top    = float(request.form.get("top",    "0"))
    bottom = float(request.form.get("bottom", "0"))
    try:
        with temp_upload(f) as path:
            doc = fitz.open(path)
            for page in doc:
                r = page.rect
                page.set_cropbox(fitz.Rect(r.x0 + left, r.y0 + top, r.x1 - right, r.y1 - bottom))
            filename = generate_output_filename(f.filename, "cropped")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            doc.save(out)
            doc.close()
        return ok("PDF pages cropped", out)
    except Exception:
        log.exception("crop")
        return err("Crop failed", 500)


@app.route("/api/info", methods=["POST"])
@rate_limited()
def pdf_info():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            doc  = fitz.open(path)
            meta = doc.metadata
            pages_info = []
            for i, pg in enumerate(doc):
                pages_info.append({"page": i + 1, "width_pt": round(pg.rect.width, 1),
                                    "height_pt": round(pg.rect.height, 1)})
            out_data = {
                "page_count": len(doc),
                "title":     meta.get("title", ""),
                "author":    meta.get("author", ""),
                "subject":   meta.get("subject", ""),
                "creator":   meta.get("creator", ""),
                "encrypted": doc.is_encrypted,
                "size_human": (f"{os.path.getsize(path)/1048576:.2f} MB"
                               if os.path.getsize(path) > 1048576
                               else f"{os.path.getsize(path)/1024:.1f} KB"),
                "page_sizes": pages_info[:5]
            }
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
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    pw  = sanitize(request.form.get("password", ""))
    pw2 = sanitize(request.form.get("password2", ""))
    if not pw:
        return err("Password required")
    if pw != pw2:
        return err("Passwords do not match")
    try:
        with temp_upload(f) as path:
            r = PdfReader(path)
            w = PdfWriter()
            for page in r.pages:
                w.add_page(page)
            w.encrypt(pw)
            filename = generate_output_filename(f.filename, "protected")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(out, "wb") as fh:
                w.write(fh)
        return ok("PDF password protected", out)
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
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    name   = sanitize(request.form.get("name", "Signed"))
    reason = sanitize(request.form.get("reason", "Approved"))
    try:
        with temp_upload(f) as path:
            doc = fitz.open(path)
            for page in doc:
                r    = page.rect
                text = f"✍ {name}  |  {reason}  |  {datetime.now().strftime('%Y-%m-%d')}"
                page.draw_rect(
                    fitz.Rect(r.x0 + 20, r.y1 - 40, r.x1 - 20, r.y1 - 10),
                    color=(0, 0, 0.6), fill=(0.9, 0.9, 1), width=0.5
                )
                page.insert_text((r.x0 + 25, r.y1 - 20), text, fontsize=9, color=(0, 0, 0.5))
            filename = generate_output_filename(f.filename, "signed")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            doc.save(out)
            doc.close()
        return ok("Signature added to all pages", out)
    except Exception:
        log.exception("sign_pdf")
        return err("Sign failed", 500)


@app.route("/api/redact-pdf", methods=["POST"])
@rate_limited()
def redact_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    search_text = sanitize(request.form.get("search_text", ""))
    if not search_text:
        return err("Search text required")
    try:
        with temp_upload(f) as path:
            doc   = fitz.open(path)
            count = 0
            for page in doc:
                hits = page.search_for(search_text)
                for rect in hits:
                    page.add_redact_annot(rect, fill=(0, 0, 0))
                    count += 1
                page.apply_redactions()
            filename = generate_output_filename(f.filename, "redacted")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            doc.save(out)
            doc.close()
        return ok(f"Redacted {count} occurrence(s)", out)
    except Exception:
        log.exception("redact")
        return err("Redact failed", 500)


@app.route("/api/compare-pdf", methods=["POST"])
@rate_limited()
def compare_pdf():
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
            pages = min(len(doc1), len(doc2))
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for i in range(pages):
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
            doc1.close()
            doc2.close()
            filename = generate_output_filename(
                files[0].filename, "comparison",
                is_multi=True, filenames=[f.filename for f in files]
            )
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(out, "wb") as fh:
                fh.write(buf.getvalue())
        return ok(f"Compared {pages} page(s) — differences highlighted", out)
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
    dpi = min(int(request.form.get("dpi", "150")), 300)
    if fmt not in ("jpg", "png"):
        fmt = "jpg"
    try:
        with temp_upload(f) as path:
            doc   = fitz.open(path)
            count = len(doc)
            buf   = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, page in enumerate(doc):
                    mat = fitz.Matrix(dpi / 72, dpi / 72)
                    pix = page.get_pixmap(matrix=mat, alpha=False)
                    zf.writestr(f"page_{i+1:04d}.{fmt}", pix.tobytes(fmt))
            doc.close()
            filename = generate_output_filename(f.filename, "to_image")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(out, "wb") as fh:
                fh.write(buf.getvalue())
        return ok(f"Exported {count} page(s) as {fmt.upper()}", out)
    except Exception:
        log.exception("pdf_to_image")
        return err("Export failed", 500)


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
        with temp_upload(f) as path:
            filename = generate_output_filename(f.filename, "to_word")
            filename = re.sub(r'\.pdf$', '.docx', filename, flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            cv  = Pdf2DocxConverter(path)
            cv.convert(out)
            cv.close()
        return ok("PDF converted to Word", out)
    except Exception:
        log.exception("pdf_to_word")
        return err("PDF to Word failed", 500)


@app.route("/api/pdf-to-excel", methods=["POST"])
@rate_limited()
def pdf_to_excel():
    if not TABULA_AVAILABLE:
        return err("PDF to Excel requires tabula-py. Install: pip install tabula-py", 501)
    if not OPENPYXL_AVAILABLE:
        return err("PDF to Excel requires openpyxl. Install: pip install openpyxl", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            dfs = tabula.read_pdf(path, pages='all', multiple_tables=True)
            wb  = Workbook()
            wb.remove(wb.active)
            for i, df in enumerate(dfs):
                ws = wb.create_sheet(title=f"Table_{i+1}")
                ws.append(list(df.columns))
                for row in df.itertuples(index=False):
                    ws.append([str(v) if v is not None else "" for v in row])
            filename = generate_output_filename(f.filename, "to_excel")
            filename = re.sub(r'\.pdf$', '.xlsx', filename, flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            wb.save(out)
        return ok(f"Extracted {len(dfs)} table(s) to Excel", out)
    except Exception:
        log.exception("pdf_to_excel")
        return err("PDF to Excel failed", 500)


@app.route("/api/pdf-to-ppt", methods=["POST"])
@rate_limited()
def pdf_to_ppt():
    if not PPTX_AVAILABLE:
        return err("PDF to PPT requires python-pptx. Install: pip install python-pptx", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            doc = fitz.open(path)
            prs = Presentation()
            prs.slide_width  = PptxInches(10)
            prs.slide_height = PptxInches(7.5)
            blank = prs.slide_layouts[6]
            for page in doc:
                pix     = page.get_pixmap(dpi=150)
                tmp_img = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                tmp_img.write(pix.tobytes("png"))
                tmp_img.close()
                slide = prs.slides.add_slide(blank)
                slide.shapes.add_picture(tmp_img.name, 0, 0, prs.slide_width, prs.slide_height)
                os.unlink(tmp_img.name)
            doc.close()
            filename = generate_output_filename(f.filename, "to_ppt")
            filename = re.sub(r'\.pdf$', '.pptx', filename, flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            prs.save(out)
        return ok("PDF converted to PowerPoint", out)
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
    version  = request.form.get("version", "1b")
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
            result = subprocess.run(cmd, capture_output=True, timeout=120)
            if result.returncode != 0:
                return err("Ghostscript PDF/A conversion failed. Is Ghostscript installed?", 500)
        return ok(f"Converted to PDF/A-{version}", out)
    except subprocess.TimeoutExpired:
        return err("PDF/A conversion timed out", 500)
    except Exception:
        log.exception("pdf_to_pdfa")
        return err("PDF/A conversion failed", 500)


# ═════════════════════════════════════════════════════════════════
# PDF CONVERT TO
# ═════════════════════════════════════════════════════════════════

def _images_to_pdf(paths: list, page_size_str: str, output_filename: str) -> str:
    size_map = {"a4": A4, "letter": letter}
    size     = size_map.get(page_size_str.lower(), None)
    out      = os.path.join(Config.OUTPUT_FOLDER, output_filename)
    c        = rl_canvas.Canvas(out, pagesize=size or letter)
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
                return err("LibreOffice conversion failed", 500)
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
            try:
                from weasyprint import HTML
                HTML(filename=path).write_pdf(out_path)
                return ok("HTML converted to PDF", out_path)
            except ImportError:
                pass
            result = subprocess.run(["wkhtmltopdf", path, out_path],
                                    capture_output=True, timeout=60)
            if result.returncode == 0:
                return ok("HTML converted to PDF", out_path)
            return err("HTML to PDF requires weasyprint or wkhtmltopdf.", 501)
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
    quality = int(request.form.get("quality", "60"))
    try:
        with temp_upload(f) as path:
            img = Image.open(path).convert("RGB")
            ext = path.rsplit(".", 1)[-1].lower()
            fmt = "JPEG" if ext in ("jpg", "jpeg", "webp") else "PNG"
            filename = generate_output_filename(f.filename, "compressed")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            img.save(out, format=fmt, quality=quality, optimize=True)
        return ok("Image compressed", out)
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
    width      = int(request.form.get("width", "800"))
    height     = int(request.form.get("height", "600"))
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
    quality = int(request.form.get("quality", "75"))
    try:
        with temp_uploads(files) as paths:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, path in enumerate(paths):
                    img = Image.open(path).convert("RGB")
                    ib  = io.BytesIO()
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
    quality = int(request.form.get("quality", "75"))
    try:
        with temp_uploads(files) as paths:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, path in enumerate(paths):
                    img = Image.open(path).convert("RGB")
                    ib  = io.BytesIO()
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
    if not DOCX_AVAILABLE:
        return err("Image to Word requires python-docx. Install: pip install python-docx", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_IMAGE)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
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
    if not OPENPYXL_AVAILABLE:
        return err("Image to Excel requires openpyxl. Install: pip install openpyxl", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_IMAGE)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            img = Image.open(path).convert("RGB")
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            img.save(tmp.name, format="PNG")
            tmp.close()
            wb     = Workbook()
            ws     = wb.active
            ws.title = "Image"
            xl_img = XlImage(tmp.name)
            xl_img.anchor = "B2"
            ws.add_image(xl_img)
            filename = generate_output_filename(f.filename, "to_excel")
            filename = re.sub(r'\.(jpg|jpeg|png|gif|bmp|tiff|webp)$', '.xlsx',
                               filename, flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            wb.save(out)
            os.unlink(tmp.name)
        return ok("Image embedded in Excel workbook", out)
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
                return err("LibreOffice conversion failed", 500)
            doc = fitz.open(pdf_path)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, page in enumerate(doc):
                    pix = page.get_pixmap(dpi=150)
                    zf.writestr(f"page_{i+1:04d}.jpg", pix.tobytes("jpeg"))
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
                return err("LibreOffice conversion failed", 500)
            doc = fitz.open(pdf_path)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, page in enumerate(doc):
                    pix = page.get_pixmap(dpi=150)
                    zf.writestr(f"page_{i+1:04d}.png", pix.tobytes("png"))
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
                doc  = DocxDocument(path)
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
                    return err("LibreOffice conversion failed", 500)
        return ok("Word converted to TXT", out)
    except Exception:
        log.exception("word_to_txt")
        return err("Word to TXT failed", 500)


@app.route("/api/word-to-excel", methods=["POST"])
@rate_limited()
def word_to_excel():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            filename = generate_output_filename(f.filename, "to_excel")
            filename = re.sub(r'\.(doc|docx)$', '.xlsx', filename, flags=re.IGNORECASE)
            out = libre(path, "xlsx", output_filename=filename)
            if not out:
                return err("LibreOffice conversion failed", 500)
        return ok("Word converted to Excel", out)
    except Exception:
        log.exception("word_to_excel")
        return err("Word to Excel failed", 500)


@app.route("/api/word-to-ppt", methods=["POST"])
@rate_limited()
def word_to_ppt():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            filename = generate_output_filename(f.filename, "to_ppt")
            filename = re.sub(r'\.(doc|docx)$', '.pptx', filename, flags=re.IGNORECASE)
            out = libre(path, "pptx", output_filename=filename)
            if not out:
                return err("LibreOffice conversion failed", 500)
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
                return err("LibreOffice conversion failed", 500)
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
            doc  = DocxDocument(path)
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
    if not DOCX_AVAILABLE:
        return err("Compress Word requires python-docx. Install: pip install python-docx", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            orig     = os.path.getsize(path)
            filename = generate_output_filename(f.filename, "compressed")
            out      = os.path.join(Config.OUTPUT_FOLDER, filename)
            if path.endswith(".docx"):
                doc = DocxDocument(path)
                doc.save(out)
            else:
                out = libre(path, "docx", output_filename=filename)
                if not out:
                    return err("Compression failed", 500)
            new_size  = os.path.getsize(out)
            reduction = round((1 - new_size / orig) * 100, 1) if orig else 0
        return ok(f"Word compressed ({reduction}% smaller)", out)
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
    pw  = sanitize(request.form.get("password", ""))
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
    find_text    = sanitize(request.form.get("find_text", ""))
    replace_text = sanitize(request.form.get("replace_text", ""))
    if not find_text:
        return err("Find text required")
    try:
        with temp_upload(f) as path:
            doc   = DocxDocument(path)
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
    if not OPENPYXL_AVAILABLE:
        return err("Excel to CSV requires openpyxl. Install: pip install openpyxl", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            wb  = load_workbook(path, data_only=True)
            ws  = wb.active
            filename = generate_output_filename(f.filename, "to_csv")
            filename = re.sub(r'\.(xls|xlsx)$', '.csv', filename, flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(out, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                for row in ws.iter_rows(values_only=True):
                    writer.writerow([str(v) if v is not None else "" for v in row])
        return ok("Excel converted to CSV", out)
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
            if not pdf_path:
                return err("LibreOffice conversion failed", 500)
            doc = fitz.open(pdf_path)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, page in enumerate(doc):
                    pix = page.get_pixmap(dpi=150)
                    zf.writestr(f"sheet_{i+1:04d}.jpg", pix.tobytes("jpeg"))
            doc.close()
            try:
                os.remove(pdf_path)
            except Exception:
                pass
            filename = generate_output_filename(f.filename, "to_jpg")
            filename = re.sub(r'\.(xls|xlsx)$', '.zip', filename, flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(out, "wb") as fh:
                fh.write(buf.getvalue())
        return ok("Excel sheets exported as JPG", out)
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
            if not pdf_path:
                return err("LibreOffice conversion failed", 500)
            doc = fitz.open(pdf_path)
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                for i, page in enumerate(doc):
                    pix = page.get_pixmap(dpi=150)
                    zf.writestr(f"sheet_{i+1:04d}.png", pix.tobytes("png"))
            doc.close()
            try:
                os.remove(pdf_path)
            except Exception:
                pass
            filename = generate_output_filename(f.filename, "to_png")
            filename = re.sub(r'\.(xls|xlsx)$', '.zip', filename, flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(out, "wb") as fh:
                fh.write(buf.getvalue())
        return ok("Excel sheets exported as PNG", out)
    except Exception:
        log.exception("excel_to_png")
        return err("Excel to PNG failed", 500)


@app.route("/api/excel-to-word", methods=["POST"])
@rate_limited()
def excel_to_word():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            filename = generate_output_filename(f.filename, "to_word")
            filename = re.sub(r'\.(xls|xlsx)$', '.docx', filename, flags=re.IGNORECASE)
            out = libre(path, "docx", output_filename=filename)
            if not out:
                return err("LibreOffice conversion failed", 500)
        return ok("Excel converted to Word", out)
    except Exception:
        log.exception("excel_to_word")
        return err("Excel to Word failed", 500)


@app.route("/api/excel-to-ppt", methods=["POST"])
@rate_limited()
def excel_to_ppt():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e:
        return err(e)
    try:
        with temp_upload(f) as path:
            filename = generate_output_filename(f.filename, "to_ppt")
            filename = re.sub(r'\.(xls|xlsx)$', '.pptx', filename, flags=re.IGNORECASE)
            out = libre(path, "pptx", output_filename=filename)
            if not out:
                return err("LibreOffice conversion failed", 500)
        return ok("Excel converted to PowerPoint", out)
    except Exception:
        log.exception("excel_to_ppt")
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
                return err("LibreOffice conversion failed", 500)
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
    try:
        with temp_upload(f) as path:
            wb   = load_workbook(path, data_only=True)
            data = {}
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                rows = []
                for row in ws.iter_rows(values_only=True):
                    rows.append([str(v) if v is not None else "" for v in row])
                data[sheet_name] = rows
            filename = generate_output_filename(f.filename, "to_json")
            filename = re.sub(r'\.(xls|xlsx)$', '.json', filename, flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        return ok(f"Excel converted to JSON ({len(data)} sheet(s))", out)
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
            wb   = load_workbook(path, data_only=True)
            filename = generate_output_filename(f.filename, "compressed")
            out  = os.path.join(Config.OUTPUT_FOLDER, filename)
            wb.save(out)
            reduction = round((1 - os.path.getsize(out) / orig) * 100, 1) if orig else 0
        return ok(f"Excel compressed ({reduction}% smaller)", out)
    except Exception:
        log.exception("compress_excel")
        return err("Excel compression failed", 500)


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
    pw  = sanitize(request.form.get("password", ""))
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
            wb  = load_workbook(path, data_only=True, read_only=False)
            filename = generate_output_filename(f.filename, "repaired")
            out = os.path.join(Config.OUTPUT_FOLDER, filename)
            wb.save(out)
        return ok("Excel workbook repaired", out)
    except Exception:
        log.exception("repair_excel")
        return err("Excel repair failed", 500)
# ─────────────────────────────────────────────────────────────────
# SEO LANDING PAGES
# ─────────────────────────────────────────────────────────────────

SEO_PAGES = {
    "merge-pdf":     {"tool": "merge",       "title": "Merge PDF Free Online — Combine PDF Files | NPKPadala", "desc": "Merge multiple PDF files into one. Free, fast, no sign-up. Combine PDFs instantly online."},
    "compress-pdf":  {"tool": "compress",     "title": "Compress PDF Free Online — Reduce PDF Size | NPKPadala", "desc": "Compress PDF files and reduce size without losing quality. Free online PDF compressor."},
    "pdf-to-word":   {"tool": "pdf-to-word",  "title": "PDF to Word Free Online — Convert PDF to DOCX | NPKPadala", "desc": "Convert PDF to editable Word document online for free. Fast and accurate PDF to DOCX converter."},
    "jpg-to-pdf":    {"tool": "jpg-to-pdf",   "title": "JPG to PDF Free Online — Convert Images to PDF | NPKPadala", "desc": "Convert JPG images to PDF online for free. Combine multiple JPGs into one PDF instantly."},
    "split-pdf":     {"tool": "split",        "title": "Split PDF Free Online — Extract PDF Pages | NPKPadala", "desc": "Split PDF into multiple files or extract specific pages. Free online PDF splitter."},
    "pdf-to-jpg":    {"tool": "pdf-to-image", "title": "PDF to JPG Free Online — Convert PDF to Image | NPKPadala", "desc": "Convert PDF pages to JPG images online for free. High quality PDF to image converter."},
    "protect-pdf":   {"tool": "protect",      "title": "Protect PDF Free Online — Password Protect PDF | NPKPadala", "desc": "Add password protection to your PDF files online for free. Secure your documents instantly."},
    "word-to-pdf":   {"tool": "word-to-pdf",  "title": "Word to PDF Free Online — Convert DOCX to PDF | NPKPadala", "desc": "Convert Word documents to PDF online for free. Fast and reliable DOCX to PDF converter."},
    "rotate-pdf":    {"tool": "rotate",       "title": "Rotate PDF Free Online — Fix PDF Orientation | NPKPadala", "desc": "Rotate PDF pages online for free. Fix portrait or landscape orientation instantly."},
    "watermark-pdf": {"tool": "watermark",    "title": "Watermark PDF Free Online — Add Text Watermark | NPKPadala", "desc": "Add custom text watermark to PDF online for free. Protect your documents with watermarks."},
}

def build_seo_page(page_slug):
    page = SEO_PAGES.get(page_slug)
    if not page:
        abort(404)
    tool_id  = page["tool"]
    title    = page["title"]
    desc     = page["desc"]
    canonical = f"https://npkpadala.com/pdfwala/{page_slug}"
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{title}</title>
<meta name="description" content="{desc}"/>
<link rel="canonical" href="{canonical}"/>
<meta property="og:title" content="{title}"/>
<meta property="og:description" content="{desc}"/>
<meta property="og:url" content="{canonical}"/>
<meta property="og:type" content="website"/>
<meta property="og:site_name" content="NPKPadala PDF Tools"/>
<meta name="twitter:card" content="summary"/>
<meta name="twitter:title" content="{title}"/>
<meta name="twitter:description" content="{desc}"/>
<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "WebApplication",
  "name": "{title}",
  "url": "{canonical}",
  "description": "{desc}",
  "applicationCategory": "UtilitiesApplication",
  "operatingSystem": "All",
  "offers": {{
    "@type": "Offer",
    "price": "0",
    "priceCurrency": "USD"
  }}
}}
</script>
<style>
  body {{ font-family: sans-serif; background: #0A0E1A; color: #fff;
         display: flex; align-items: center; justify-content: center;
         height: 100vh; margin: 0; text-align: center; }}
  .loader {{ font-size: 18px; color: #60A5FA; }}
  .spinner {{ width: 40px; height: 40px; border: 4px solid #1A2235;
              border-top: 4px solid #3B82F6; border-radius: 50%;
              animation: spin 0.8s linear infinite; margin: 0 auto 16px; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
</style>
</head>
<body>
<div class="loader">
  <div class="spinner"></div>
  Loading tool…
</div>
<script>
  sessionStorage.setItem('autoOpenTool', '{tool_id}');
  window.location.href = 'https://npkpadala.com/#tools';
</script>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html"}

@app.route("/pdfwala/<slug>")
def seo_page(slug):
    if slug not in SEO_PAGES:
        abort(404)
    return build_seo_page(slug)


@app.route("/sitemap.xml")
def sitemap():
    from datetime import date
    today = date.today().isoformat()
    urls = ["https://npkpadala.com/"] + [f"https://npkpadala.com/pdfwala/{slug}" for slug in SEO_PAGES]
    url_blocks = "\n".join([
        f"""  <url>
    <loc>{u}</loc>
    <lastmod>{today}</lastmod>
    <changefreq>weekly</changefreq>
    <priority>{"1.0" if u == "https://npkpadala.com/" else "0.8"}</priority>
  </url>""" for u in urls
    ])
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{url_blocks}
</urlset>"""
    return xml, 200, {"Content-Type": "application/xml"}


@app.route("/robots.txt")
def robots():
    content = """User-agent: *
Allow: /
Disallow: /api/
Disallow: /download/

Sitemap: https://npkpadala.com/sitemap.xml"""
    return content, 200, {"Content-Type": "text/plain"}

    
# ─────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
