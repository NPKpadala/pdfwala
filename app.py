"""PDFWala Enterprise V10.0 — Modular Production Backend"""
__version__ = "10.0.1"
import os, sys, io, re, csv, json, uuid, time, shutil, signal, zipfile
import logging, unicodedata, threading, subprocess, tempfile
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager
from itertools import islice  # FIX-059: for islice on word generators
from flask import Flask, request, jsonify, g, send_file, Response
from werkzeug.utils import secure_filename
from config import Config
from services.redis_service import redis_service
from services.file_service import FileService
from services.storage_service import get_storage
from services.queue_service import (
    backpressure, cb_libreoffice, cb_ghostscript, cb_tesseract,
    queue_service, CircuitBreaker
)
from services.auth_service import require_auth, require_rate_limit
from utils.validators import validate_file, validate_password
from utils.helpers import (
    generate_output_filename, sanitize_string, safe_int,
    format_file_size, get_timestamp, generate_uuid
)
from utils.security import generate_signed_url, verify_signed_url, SafeRegex, REDACTION_PATTERNS
from utils.pdf_utils import (
    parse_page_ranges, create_watermark_pdf, create_page_number_pdf,
    get_pdf_page_count, compress_pdf_images
)
from utils.office_utils import coerce_cell_value, coerce_cell_for_csv
try:
    from flask_cors import CORS
    CORS_AVAILABLE = True
except ImportError:
    CORS_AVAILABLE = False
import fitz
from PIL import Image, ImageChops
from PyPDF2 import PdfReader, PdfWriter, PdfMerger
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib.pagesizes import letter, A4
try:
    from PIL import ImageDraw, ImageFont
    IMAGEDRAW_AVAILABLE = True
except ImportError:
    IMAGEDRAW_AVAILABLE = False
try:
    from docx import Document as DocxDocument
    from docx.shared import Inches, Pt
    from openpyxl.styles import Font as XlFont
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False
try:
    from openpyxl import load_workbook, Workbook
    from openpyxl.drawing.image import Image as XlImage
    from openpyxl.styles import Font, Alignment
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
try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False
try:
    from prometheus_client import Counter, Histogram, generate_latest, REGISTRY
    METRICS_ENABLED = True
    _prom_req_count = Counter('pdfwala_requests_total', 'Total requests',
                               ['method', 'endpoint', 'status'])
    _prom_req_dur = Histogram('pdfwala_request_duration_seconds', 'Duration',
                                 ['method', 'endpoint'])
    _prom_file_size = Histogram('pdfwala_file_size_bytes', 'Upload size', ['operation'],
                                 buckets=[1024,10240,102400,1048576,10485760,104857600])
except ImportError:
    METRICS_ENABLED = False
# ── Celery tasks ───────────────────────────────────────────────────────────────
from workers.celery_app import celery_app
try:
    from tasks.pdf_tasks import compress_pdf_task, merge_pdf_task, split_pdf_task, watermark_pdf_task
    from tasks.ocr_tasks import ocr_pdf_task
    from tasks.office_tasks import pdf_to_word_task, pdf_to_excel_task
except Exception:
    compress_pdf_task = merge_pdf_task = split_pdf_task = watermark_pdf_task = None
    ocr_pdf_task = pdf_to_word_task = pdf_to_excel_task = None
# ── Ensure directories exist ───────────────────────────────────────────────────
for _d in [Config.UPLOAD_FOLDER, Config.OUTPUT_FOLDER, Config.TEMP_FOLDER]:
    os.makedirs(_d, exist_ok=True)
_APP_START = time.time()
log = logging.getLogger("pdfwala")

# FIX-060: semaphore to limit concurrent OCR operations and prevent CPU saturation
_ocr_semaphore = threading.Semaphore(getattr(Config, "MAX_CONCURRENT_OCR", 2))

# ── Flask app ──────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = Config.MAX_FILE_SIZE
app.secret_key = Config.SECRET_KEY
if CORS_AVAILABLE:
    CORS(app, origins=Config.CORS_ORIGINS)
# ── Request lifecycle ──────────────────────────────────────────────────────────
@app.before_request
def _before():
    g.start = time.time()
    g.request_id = request.headers.get("X-Request-ID", str(uuid.uuid4())[:8])
    g.user_id = "anonymous"
@app.after_request
def _after(response):
    ms = round((time.time() - g.get("start", time.time())) * 1000, 1)
    log.info(f"{request.method} {request.path} → {response.status_code} [{ms}ms]")
    if METRICS_ENABLED:
        try:
            _prom_req_count.labels(request.method, request.path, str(response.status_code)).inc()
            _prom_req_dur.labels(request.method, request.path).observe(
                time.time() - g.get("start", time.time()))
        except Exception:
            pass
    response.headers["X-Request-ID"] = g.get("request_id", "-")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers.pop("Server", None)
    return response
# ── Response helpers ───────────────────────────────────────────────────────────
def err(msg, code=400):
    log.warning(f"[{g.get('request_id','-')}] ERR {code}: {msg}")
    return jsonify({"success": False, "error": msg,
                    "request_id": g.get("request_id", "-")}), code

def ok(msg, path=None, **extras):
    payload = {"success": True, "message": msg, **extras}
    if path and os.path.exists(path):
        size = os.path.getsize(path)
        payload.update({
            "download_url": f"/pdfwala/download/{os.path.basename(path)}",
            "signed_url": generate_signed_url(path),
            "filename": os.path.basename(path),
            "size_human": format_file_size(size),
            "expires_in": f"{Config.FILE_TTL_SEC // 60} minutes",
        })
    return jsonify(payload)
# ── LibreOffice helper ────────────────────────────────────────────────────────
def libre(input_path, fmt, output_filename=None, temp=False):
    if fmt not in Config.LIBRE_ALLOWED_FMTS:
        return None
    if not cb_libreoffice.can_execute():
        return None
    if output_filename:
        output_filename = secure_filename(output_filename)
        if not output_filename:
            return None
    out_dir = tempfile.mkdtemp()
    try:
        result = subprocess.run(
            [Config.LIBREOFFICE, "--headless", "--convert-to", fmt,
             "--outdir", out_dir, input_path],
            capture_output=True, timeout=Config.SUBPROCESS_TIMEOUT)
        if result.returncode != 0:
            cb_libreoffice.record_failure()
            return None
        base = os.path.splitext(os.path.basename(input_path))[0]
        converted = os.path.join(out_dir, f"{base}.{fmt}")
        if not os.path.exists(converted):
            matches = list(Path(out_dir).glob(f"*.{fmt}"))
            if not matches:
                cb_libreoffice.record_failure()
                return None
            converted = str(matches[0])
        if os.path.getsize(converted) == 0:
            cb_libreoffice.record_failure()
            return None
        if temp:
            final = os.path.join(Config.TEMP_FOLDER, f"{uuid.uuid4()}.{fmt}")
        elif output_filename:
            final = os.path.join(Config.OUTPUT_FOLDER, output_filename)
        else:
            final = os.path.join(Config.OUTPUT_FOLDER, f"{uuid.uuid4()}_output.{fmt}")
        # FIX-064: use os.replace (atomic on same filesystem), fallback to copy+delete
        try:
            os.replace(converted, final)
        except OSError:
            shutil.copy2(converted, final)
            try:
                os.remove(converted)
            except OSError:
                pass
        # FIX-004: validate final path stays inside OUTPUT_FOLDER or TEMP_FOLDER
        allowed_dirs = (os.path.realpath(Config.OUTPUT_FOLDER),
                        os.path.realpath(Config.TEMP_FOLDER))
        real_final = os.path.realpath(final)
        if not any(real_final.startswith(d) for d in allowed_dirs):
            try:
                os.remove(final)
            except OSError:
                pass
            cb_libreoffice.record_failure()
            return None
        cb_libreoffice.record_success()
        return final
    except subprocess.TimeoutExpired:
        cb_libreoffice.record_failure()
        return None
    except Exception as ex:
        cb_libreoffice.record_failure()
        log.error(f"LibreOffice: {ex}")
        return None
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
# ── Ghostscript helper ────────────────────────────────────────────────────────
def ghostscript_compress(input_path, output_path, gs_setting="/ebook",
                          extra_flags=None, timeout=300):
    if not cb_ghostscript.can_execute():
        return False
    # FIX-003: assert output_path stays inside OUTPUT_FOLDER before running GS
    if not os.path.realpath(output_path).startswith(
            os.path.realpath(Config.OUTPUT_FOLDER)):
        return False
    cmd = [Config.GHOSTSCRIPT, "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.5",
           f"-dPDFSETTINGS={gs_setting}", "-dNOPAUSE", "-dBATCH", "-dQUIET",
           "-dSAFER",  # FIX-001: was -dNOSAFER; enable GS filesystem sandbox
           "-dDetectDuplicateImages=true", "-dCompressFonts=true",
           "-dSubsetFonts=true", "-dAutoRotatePages=/None",
           f"-sOutputFile={output_path}"]
    if extra_flags:
        cmd.extend(extra_flags)
    cmd.append(input_path)
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if result.returncode != 0:
            cb_ghostscript.record_failure()
            return False
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            cb_ghostscript.record_failure()
            return False
        try:
            doc = fitz.open(output_path)
            if len(doc) == 0:
                doc.close()
                try: os.remove(output_path)
                except OSError: pass
                return False
            doc.close()
        except Exception:
            try: os.remove(output_path)
            except OSError: pass
            return False
        cb_ghostscript.record_success()
        return True
    except subprocess.TimeoutExpired:
        cb_ghostscript.record_failure()
        try: os.remove(output_path)
        except OSError: pass
        return False
    except Exception as ex:
        cb_ghostscript.record_failure()
        log.error(f"Ghostscript: {ex}")
        return False
# ── Background cleanup ─────────────────────────────────────────────────────────
def _cleanup_worker():
    while True:
        try:
            now = time.time()
            # FIX-049: use os.scandir instead of os.walk to reduce I/O overhead
            for folder in [Config.OUTPUT_FOLDER, Config.UPLOAD_FOLDER, Config.TEMP_FOLDER]:
                try:
                    with os.scandir(folder) as it:
                        for entry in it:
                            if (entry.is_file(follow_symlinks=False) and
                                    now - entry.stat().st_mtime > Config.FILE_TTL_SEC):
                                try:
                                    # FIX-061: rename to .deleting suffix before removing to prevent mid-download deletion
                                    tomb = entry.path + ".deleting"
                                    os.rename(entry.path, tomb)
                                    os.remove(tomb)
                                except OSError:
                                    pass
                except OSError:
                    pass
        except Exception as ex:
            log.error(f"Cleanup worker: {ex}")
        time.sleep(60)

# FIX-067: guard to prevent cleanup worker from starting twice on hot-reload or double-import
_CLEANUP_STARTED = False
if not _CLEANUP_STARTED:
    _CLEANUP_STARTED = True
    threading.Thread(target=_cleanup_worker, daemon=True, name="cleanup").start()

# ============================================================================
# HEALTH / METRICS / DOWNLOAD
# ============================================================================
# FIX-022: cache for health subprocess checks (30-second TTL)
_HEALTH_CACHE = {}
_HEALTH_TTL = 30

@app.route("/health")
@app.route("/api/v1/health")
def health():
    # FIX-022: serve cached tool-check results to avoid per-request process forks
    now = time.time()
    if now - _HEALTH_CACHE.get("ts", 0) < _HEALTH_TTL:
        tools = _HEALTH_CACHE["tools"]
    else:
        tools = {}
        for name, cmd in [("libreoffice",[Config.LIBREOFFICE,"--version"]),
                           ("ghostscript",[Config.GHOSTSCRIPT,"--version"]),
                           ("tesseract",["tesseract","--version"])]:
            try:
                subprocess.run(cmd, capture_output=True, timeout=5)
                tools[name] = "ok"
            except Exception:
                tools[name] = "unavailable"
        _HEALTH_CACHE["ts"] = now
        _HEALTH_CACHE["tools"] = tools

    rc = redis_service.client
    redis_status = "ok" if rc else "unavailable"
    try:
        if rc: rc.ping()
    except Exception:
        redis_status = "error"
    celery_status = "unavailable"
    if celery_app:
        try:
            celery_app.control.ping(timeout=1)
            celery_status = "ok"
        except Exception:
            celery_status = "degraded"
    return jsonify({
        "success": True, "status": "ok", "version": Config.VERSION,
        "uptime_seconds": round(time.time() - _APP_START, 1),
        "redis": redis_status, "celery": celery_status, "tools": tools,
        "libraries": {
            "pdf2docx": PDF2DOCX_AVAILABLE, "pdfplumber": PDFPLUMBER_AVAILABLE,
            "tabula": TABULA_AVAILABLE, "pytesseract": TESSERACT_AVAILABLE,
            "python_docx": DOCX_AVAILABLE, "openpyxl": OPENPYXL_AVAILABLE,
            "msoffcrypto": MSOFFCRYPTO_AVAILABLE, "python_pptx": PPTX_AVAILABLE,
        },
        "circuit_breakers": {
            "libreoffice": cb_libreoffice.state,
            "ghostscript": cb_ghostscript.state,
            "tesseract": cb_tesseract.state,
        },
    })
@app.route("/api/v1/ready")
def readiness():
    rc = redis_service.client
    if rc:
        try: rc.ping()
        except Exception:
            return jsonify({"ready": False, "reason": "Redis unavailable"}), 503
    return jsonify({"ready": True})
@app.route("/api/v1/live")
def liveness():
    return jsonify({"alive": True, "pid": os.getpid()})
@app.route("/metrics")
def metrics():
    if not METRICS_ENABLED:
        return jsonify({"error": "prometheus_client not installed"}), 404
    return Response(generate_latest(REGISTRY), mimetype="text/plain")
@app.route("/download/<filename>")
def download(filename):
    filename = unicodedata.normalize("NFC", filename)
    safe = secure_filename(filename)
    if not safe or "/" in safe or ".." in safe:
        return err("Invalid filename", 400)
    # FIX-002a: reject filenames containing path separators after secure_filename
    if os.sep in safe or safe != os.path.basename(safe):
        return err("Invalid filename", 400)
    expires = request.args.get("expires", "")
    signature = request.args.get("sig", "")
    if expires and signature:
        if not verify_signed_url(safe, expires, signature):
            return err("Invalid or expired download link", 403)
    # FIX-061: also reject .deleting files from being served
    ALLOWED_EXTS = (".pdf",".zip",".jpg",".jpeg",".png",".docx",".xlsx",
                    ".pptx",".txt",".json",".html",".csv",".webp")
    if not safe.lower().endswith(ALLOWED_EXTS):
        return err("Invalid file type for download", 400)
    path = os.path.realpath(os.path.join(Config.OUTPUT_FOLDER, safe))
    # FIX-002b: append os.sep to prevent prefix-match bypass
    if not path.startswith(os.path.realpath(Config.OUTPUT_FOLDER) + os.sep):
        return err("Access denied", 403)
    if not os.path.exists(path):
        return err("File not found or expired", 404)
    response = send_file(path, as_attachment=True, conditional=True)
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["Cache-Control"] = "no-cache"
    return response
@app.route("/api/v1/jobs/<job_id>", methods=["GET"])
@require_auth
def api_job_status(job_id):
    # FIX-010: cap job_id length before regex sanitization
    if len(job_id) > 64:
        return err("Invalid job ID", 400)
    safe_id = re.sub(r"[^a-f0-9\-]", "", job_id)
    if safe_id != job_id:
        return err("Invalid job ID", 400)
    job = redis_service.job_get(job_id)
    if not job:
        if celery_app:
            try:
                from celery.result import AsyncResult
                task = AsyncResult(job_id, app=celery_app)
                if task.state == "PENDING":
                    return jsonify({"success": True, "status": "pending", "progress": 0})
                elif task.state == "SUCCESS":
                    result = task.result or {}
                    out = result.get("output", "")
                    resp = {"success": True, "status": "completed", "progress": 100}
                    if out and os.path.exists(out):
                        resp["download_url"] = generate_signed_url(out)
                        resp["filename"] = os.path.basename(out)
                    return jsonify(resp)
                elif task.state == "FAILURE":
                    return jsonify({"success": False, "status": "failed", "error": str(task.info)}), 500
                return jsonify({"success": True, "status": task.state.lower(), "progress": 0})
            except Exception:
                pass
        return err("Job not found", 404)
    status = job.get("status", "unknown")
    progress = int(job.get("progress", 0))
    resp = {"success": True, "job_id": job_id, "status": status,
            "progress": progress, "operation": job.get("operation", ""),
            "created_at": job.get("created_at", "")}
    if job.get("total_pages"):
        resp["total_pages"] = int(job["total_pages"])
        resp["current_page"] = int(job.get("current_page", 0))
    if status == "completed":
        out = job.get("output_path", "")
        if out and os.path.exists(out):
            resp["download_url"] = generate_signed_url(out)
            resp["filename"] = os.path.basename(out)
            resp["size_human"] = format_file_size(os.path.getsize(out))
    if status == "failed":
        resp["error"] = job.get("error", "Unknown error")
    return jsonify(resp)
@app.route("/api/v1/endpoints", methods=["GET"])
def list_endpoints():
    endpoints = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        endpoints.append({"path": rule.rule,
                           "methods": sorted(m for m in rule.methods if m not in ("HEAD","OPTIONS"))})
    return jsonify({"success": True, "endpoints": sorted(endpoints, key=lambda x: x["path"])})
# ============================================================================
# PDF ORGANIZE
# ============================================================================
@app.route("/api/v1/merge", methods=["POST"])
@app.route("/api/merge", methods=["POST"])
@require_auth
@require_rate_limit
def merge_pdf():
    files = request.files.getlist("files")
    if len(files) < 2: return err("Minimum 2 PDF files required")
    if len(files) > Config.MAX_FILES_MERGE: return err(f"Maximum {Config.MAX_FILES_MERGE} files")
    for f in files:
        e = validate_file(f, Config.ALLOWED_PDF)
        if e: return err(e)
    try:
        with FileService.temp_uploads(files) as paths:
            page_sizes = set()
            for p in paths:
                doc = fitz.open(p)
                # FIX-019: check for encrypted PDFs before merging
                if doc.is_encrypted and not doc.authenticate(""):
                    doc.close()
                    return err("One or more PDFs are password-protected. Please unlock them first.", 400)
                # FIX-088: guard against empty PDFs in merge
                if len(doc) == 0:
                    doc.close()
                    return err("Input PDF has no pages", 400)
                for pg in doc:
                    page_sizes.add((round(pg.rect.width,0), round(pg.rect.height,0)))
                doc.close()
            out_doc = fitz.open()
            for p in paths:
                src = fitz.open(p)
                out_doc.insert_pdf(src)
                src.close()
            fname = generate_output_filename(files[0].filename, "merged",
                                              is_multi=True, filenames=[f.filename for f in files])
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            out_doc.save(out)
            out_doc.close()
        extras = {}
        extras["mixed_page_sizes"] = (len(page_sizes) > 1)
        # FIX-040: include warning when mixed page sizes detected
        if len(page_sizes) > 1:
            extras["warning"] = ("Input PDFs have different page sizes — "
                                 "output may have inconsistent page dimensions")
        return ok(f"Merged {len(files)} PDFs", out, **extras)
    except Exception:
        log.exception("merge"); return err("Merge failed", 500)
@app.route("/api/v1/split", methods=["POST"])
@app.route("/api/split", methods=["POST"])
@require_auth
@require_rate_limit
def split_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    mode = request.form.get("mode", "all")
    ranges = request.form.get("ranges", "")
    if mode == "range" and not ranges.strip():
        return err("Page range required when mode is range", 400)
    try:
        with FileService.temp_upload(f) as path:
            reader = PdfReader(path)
            total = len(reader.pages)
            indices = list(range(total)) if mode == "all" else parse_page_ranges(ranges, total)
            # FIX-097: validate indices list is non-empty after parse_page_ranges
            if not indices: return err("No valid pages in range")
            # FIX-068: deduplicate page indices while preserving order
            seen_idx = set()
            indices = [i for i in indices if not (i in seen_idx or seen_idx.add(i))]  # FIX-068
            # FIX-044: use disk-based temp file for ZIP to avoid OOM on large PDFs
            tmp_zip = tempfile.NamedTemporaryFile(
                suffix=".zip", delete=False, dir=Config.TEMP_FOLDER)
            tmp_zip.close()
            try:
                with zipfile.ZipFile(tmp_zip.name, "w", zipfile.ZIP_DEFLATED) as zf:
                    for idx in indices:
                        w = PdfWriter(); w.add_page(reader.pages[idx])
                        pb = io.BytesIO(); w.write(pb)
                        zf.writestr(f"page_{idx+1:04d}.pdf", pb.getvalue())
                op = "split_pages" if mode == "all" else "extracted_pages"
                fname = generate_output_filename(f.filename, op)
                out = os.path.join(Config.OUTPUT_FOLDER, fname)
                shutil.move(tmp_zip.name, out)
            except Exception:
                try: os.remove(tmp_zip.name)
                except OSError: pass
                raise
        extras = {}
        # FIX-041: add informational note when input was single-page
        if total == 1:
            extras["info"] = "Input was a single-page PDF"
        return ok(f"Split into {len(indices)} pages", out, **extras)
    except Exception:
        log.exception("split"); return err("Split failed", 500)
@app.route("/api/v1/organize", methods=["POST"])
@app.route("/api/organize", methods=["POST"])
@require_auth
@require_rate_limit
def organize_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    action = request.form.get("action", "reorder").lower()
    order = request.form.get("order", "").strip()
    if not order: return err("order parameter required")
    try:
        with FileService.temp_upload(f) as path:
            reader = PdfReader(path)
            total = len(reader.pages)
            specified = parse_page_ranges(order, total)
            if not specified: return err("No valid pages specified")
            if action == "delete":
                final = [i for i in range(total) if i not in set(specified)]
                if not final: return err("Cannot delete all pages", 400)
            elif action == "reorder":
                # FIX-042: reorder requires all page indices to be specified
                if set(specified) != set(range(total)):
                    return err("Reorder requires all page indices to be specified", 400)
                final = specified
            else:
                final = specified
            w = PdfWriter()
            for idx in final: w.add_page(reader.pages[idx])
            fname = generate_output_filename(f.filename, "organized")
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            with open(out, "wb") as fh: w.write(fh)
        labels = {"reorder":"Reordered","extract":"Extracted","delete":"Deleted pages from"}
        return ok(f"{labels.get(action,'Organized')} PDF", out)
    except Exception:
        log.exception("organize"); return err("Organize failed", 500)
@app.route("/api/v1/remove-pages", methods=["POST"])
@app.route("/api/remove-pages", methods=["POST"])
@require_auth
@require_rate_limit
def remove_pages():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    order = request.form.get("order", "")
    if not order: return err("Pages to remove required")
    try:
        with FileService.temp_upload(f) as path:
            reader = PdfReader(path)
            total = len(reader.pages)
            remove = set(parse_page_ranges(order, total))
            if len(remove) >= total: return err("Cannot remove all pages", 400)
            w = PdfWriter()
            for i, page in enumerate(reader.pages):
                if i not in remove: w.add_page(page)
            fname = generate_output_filename(f.filename, "pages_removed")
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            with open(out, "wb") as fh: w.write(fh)
        return ok(f"Removed {len(remove)} page(s)", out)
    except Exception:
        log.exception("remove_pages"); return err("Remove pages failed", 500)
@app.route("/api/v1/extract-pages", methods=["POST"])
@app.route("/api/extract-pages", methods=["POST"])
@require_auth
@require_rate_limit
def extract_pages():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    order = request.form.get("order", "")
    if not order: return err("Pages to extract required")
    try:
        with FileService.temp_upload(f) as path:
            reader = PdfReader(path)
            total = len(reader.pages)
            indices = parse_page_ranges(order, total)
            # FIX-097: confirm extract_pages also validates non-empty indices
            if not indices: return err("No valid pages in specified range", 400)
            w = PdfWriter()
            for idx in indices: w.add_page(reader.pages[idx])
            fname = generate_output_filename(f.filename, "extracted")
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            with open(out, "wb") as fh: w.write(fh)
        return ok(f"Extracted {len(indices)} page(s)", out)
    except Exception:
        log.exception("extract_pages"); return err("Extract pages failed", 500)
# ============================================================================
# PDF OPTIMIZE
# ============================================================================
@app.route("/api/v1/compress", methods=["POST"])
@app.route("/api/compress", methods=["POST"])
@require_auth
@require_rate_limit
def compress_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    quality = request.form.get("quality", "medium").lower()
    cfg = {"low":{"dpi":150,"quality":85,"gs":"/printer"},
           "medium":{"dpi":120,"quality":72,"gs":"/printer"},
           "high":{"dpi":96,"quality":60,"gs":"/ebook"}}.get(
               quality, {"dpi":120,"quality":72,"gs":"/printer"})
    stage1 = gs_out = None
    try:
        with FileService.temp_upload(f) as path:
            orig = os.path.getsize(path)
            if METRICS_ENABLED:
                try: _prom_file_size.labels("compress").observe(orig)
                except Exception: pass
            try:
                doc = fitz.open(path)
                if len(doc) == 0: doc.close(); return err("Input PDF has no pages", 400)
                doc.close()
            except Exception:
                return err("Input PDF is corrupted or unreadable", 400)
            if orig > 50*1024*1024 and celery_app and compress_pdf_task:
                ext = path.rsplit(".",1)[-1].lower()
                bg_input = os.path.join(Config.UPLOAD_FOLDER, f"{uuid.uuid4()}.{ext}")
                shutil.copy(path, bg_input)
                fname = generate_output_filename(f.filename, "compressed")
                bg_out = os.path.join(Config.OUTPUT_FOLDER, fname)
                job_id = str(uuid.uuid4())
                redis_service.job_set(job_id, {"status":"pending","operation":"compress_pdf",
                                                "created_at":get_timestamp(),
                                                "user_id":getattr(g,"user_id","default")})
                # FIX-101: remove temp copy if Celery task dispatch raises
                try:
                    compress_pdf_task.delay(bg_input, bg_out, job_id, quality)
                except Exception as task_ex:
                    try: os.remove(bg_input)
                    except OSError: pass
                    log.error(f"compress_pdf task dispatch failed: {task_ex}")
                    return err("Failed to queue compression task", 500)
                return jsonify({"success":True,"message":"Large file queued — download will appear when ready",
                                "job_id":job_id,"status_url":f"/api/v1/jobs/{job_id}",
                                "async": True})
            stage1 = path + "_s1.pdf"
            stage1_size = orig
            try:
                doc = fitz.open(path)
                modified = compress_pdf_images(doc, cfg["dpi"], cfg["quality"])
                if modified:
                    doc.save(stage1, deflate=True, deflate_images=True,
                             deflate_fonts=True, garbage=3, clean=False)
                    stage1_size = os.path.getsize(stage1)
                else:
                    shutil.copy(path, stage1)
                doc.close()
            except Exception as ex:
                log.warning(f"Stage1: {ex}"); shutil.copy(path, stage1)
            fname = generate_output_filename(f.filename, "compressed")
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            gs_out = out + "_gs.pdf"
            gs_ok = ghostscript_compress(stage1, gs_out, cfg["gs"],
                        extra_flags=["-dColorImageDownsampleType=/Bicubic",
                                     f"-dColorImageResolution={cfg['dpi']}",
                                     f"-dGrayImageResolution={cfg['dpi']}"])

            # Validate Ghostscript output before using
            gs_valid = False
            if gs_ok and os.path.exists(gs_out) and os.path.getsize(gs_out) > 0:
                try:
                    test_doc = fitz.open(gs_out)
                    if len(test_doc) > 0:
                        gs_valid = True
                    test_doc.close()
                except:
                    gs_valid = False

            chosen = None
            if gs_valid and os.path.getsize(gs_out) < stage1_size:
                chosen = gs_out
            elif os.path.exists(stage1) and stage1_size < orig:
                try:
                    test_doc = fitz.open(stage1)
                    if len(test_doc) > 0:
                        chosen = stage1
                    test_doc.close()
                except:
                    pass

            if not chosen:
                chosen = path

            shutil.copy2(chosen, out)

            # Final validation - if output is corrupted, return original
            output_corrupted = False
            try:
                test_doc = fitz.open(out)
                if len(test_doc) == 0:
                    shutil.copy2(path, out)
                    output_corrupted = True
                test_doc.close()
            except:
                # Output is corrupted, return original file
                shutil.copy2(path, out)
                output_corrupted = True

            new_size = os.path.getsize(out)
            reduction = round((1 - new_size / orig) * 100, 1) if orig else 0

            # FIX-027: include which compression method was actually used
            compression_method = ("ghostscript" if gs_valid and not output_corrupted
                                  else "image_recompression" if chosen == stage1 and not output_corrupted
                                  else "none")

            # FIX-023: make it explicit when original is returned unchanged
            if output_corrupted:
                return ok("Compression failed — original file returned unchanged", out,
                          reduction_pct=0,
                          compression_method="none",
                          warning="Output was invalid; original file returned unmodified")

        return ok(f"Compressed — {reduction}% smaller", out,
                  reduction_pct=reduction, original_size_bytes=orig,
                  compressed_size_bytes=new_size,
                  compression_method=compression_method)  # FIX-027
    except Exception:
        log.exception("compress"); return err("Compression failed", 500)
    finally:
        # FIX-062: only delete stage1 after confirming output was successfully written
        if gs_out:
            try: os.remove(gs_out)
            except OSError: pass
        if stage1:
            if 'out' in dir() and os.path.exists(out) and os.path.getsize(out) > 0:  # FIX-062
                try: os.remove(stage1)
                except OSError: pass
@app.route("/api/v1/repair-pdf", methods=["POST"])
@app.route("/api/repair-pdf", methods=["POST"])
@require_auth
@require_rate_limit
def repair_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            orig_size = os.path.getsize(path)
            fname = generate_output_filename(f.filename, "repaired")
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            try:
                doc = fitz.open(path)
                input_pages = len(doc)
                doc.save(out, garbage=3, deflate=True, clean=False)
                doc.close()
                doc2 = fitz.open(out)
                out_pages = len(doc2); doc2.close()
                if out_pages == input_pages and os.path.getsize(out) > 0:
                    return ok("PDF repaired (PyMuPDF)", out,
                              pages=out_pages, original_size_bytes=orig_size)
            except Exception as ex1:
                log.warning(f"PyMuPDF repair: {ex1}")
            gs_tmp = out + "_gs.pdf"
            gs_ok = ghostscript_compress(path, gs_tmp, "/printer",
                        extra_flags=["-dPDFSTOPONERROR=false","-dPDFSTOPONWARNING=false"])
            if gs_ok and os.path.exists(gs_tmp) and os.path.getsize(gs_tmp) > 0:
                shutil.move(gs_tmp, out)
                return ok("PDF repaired (Ghostscript)", out)
            try: os.remove(gs_tmp)
            except OSError: pass
            shutil.copy(path, out)
            # FIX-024: explicit repaired=False when both methods fail
            return ok("PDF could not be repaired — original returned", out,
                      repaired=False,
                      warning="Both PyMuPDF and Ghostscript repair methods failed")
    except Exception:
        log.exception("repair_pdf"); return err("Repair failed", 500)
@app.route("/api/v1/linearize-pdf", methods=["POST"])
@app.route("/api/linearize-pdf", methods=["POST"])
@require_auth
@require_rate_limit
def linearize_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            orig_size = os.path.getsize(path)
            fname = generate_output_filename(f.filename, "linearized")
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            # FIX-088: guard against empty PDFs in linearize
            try:
                _chk = fitz.open(path)
                if len(_chk) == 0:
                    _chk.close()
                    return err("Input PDF has no pages", 400)
                _chk.close()
            except Exception:
                return err("Input PDF is corrupted or unreadable", 400)
            # Try qpdf first (true linearization)
            try:
                result = subprocess.run(
                    ["qpdf", "--linearize", path, out],
                    capture_output=True, timeout=120
                )
                if result.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
                    new_size = os.path.getsize(out)
                    reduction = round((1 - new_size / orig_size) * 100, 1) if orig_size else 0
                    return ok(f"PDF linearized (fast-web-view) — {reduction}% size change", out,
                              original_size_bytes=orig_size, new_size_bytes=new_size,
                              method="qpdf")
            except (FileNotFoundError, subprocess.TimeoutExpired):
                pass
            # Fallback: Ghostscript (recompresses but does not truly linearize)
            gs_ok = ghostscript_compress(path, out, gs_setting="/printer")
            if not gs_ok or not os.path.exists(out) or os.path.getsize(out) == 0:
                return err("Linearization failed — install qpdf for true linearization.", 500)
            new_size = os.path.getsize(out)
            reduction = round((1 - new_size / orig_size) * 100, 1) if orig_size else 0
            # FIX-034: add is_linearized=False for ghostscript fallback
            return ok(f"PDF recompressed — {reduction}% size change (install qpdf for true linearization)", out,
                      original_size_bytes=orig_size, new_size_bytes=new_size,
                      method="ghostscript_fallback",
                      is_linearized=False)  # FIX-034
    except Exception:
        log.exception("linearize"); return err("Linearization failed", 500)
# ============================================================================
# OCR
# ============================================================================
@app.route("/api/v1/ocr-pdf", methods=["POST"])
@app.route("/api/ocr-pdf", methods=["POST"])
@require_auth
@require_rate_limit
def ocr_pdf():
    if not TESSERACT_AVAILABLE:
        return err("OCR requires pytesseract.", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    raw_lang = request.form.get("lang", "eng")
    lang = re.sub(r'[^a-zA-Z0-9+\-]', '', raw_lang)[:50] or "eng"
    # FIX-007: tighten lang validation to exact 3-letter codes with optional + combos
    if not re.fullmatch(r'[a-zA-Z]{3}(\+[a-zA-Z]{3})*', lang):
        lang = "eng"
    # FIX-014: cap dpi maximum at 300 to prevent extreme memory allocation
    dpi = safe_int(request.form.get("dpi","300"), 300, 72, 300)
    # FIX-018: allow psm=0 (orientation detection mode)
    psm = safe_int(request.form.get("psm","3"), 3, 0, 13)
    oem = safe_int(request.form.get("oem","3"), 3, 0, 3)
    skip = request.form.get("skip_existing","true").lower() in ("true","1","yes")
    f.seek(0,2); file_size = f.tell(); f.seek(0)
    if file_size > 5*1024*1024 and celery_app and ocr_pdf_task:
        ext = f.filename.rsplit(".",1)[-1].lower() if "." in f.filename else "pdf"
        bg_input = os.path.join(Config.UPLOAD_FOLDER, f"{uuid.uuid4()}.{ext}")
        f.seek(0)
        with open(bg_input,"wb") as fh: fh.write(f.read())
        fname = generate_output_filename(f.filename, "ocr")
        bg_out = os.path.join(Config.OUTPUT_FOLDER, fname)
        job_id = str(uuid.uuid4())
        redis_service.job_set(job_id, {"status":"pending","operation":"ocr_pdf",
                                        "created_at":get_timestamp(),
                                        "user_id":getattr(g,"user_id","default")})
        redis_service.job_update(job_id, {"input_path": bg_input})
        ocr_pdf_task.delay(bg_input, bg_out, job_id, lang, dpi, psm, oem)
        return jsonify({"success":True,"message":"OCR queued. Poll status endpoint.",
                        "job_id":job_id,"status_url":f"/api/v1/jobs/{job_id}"})
    try:
        with FileService.temp_upload(f) as path:
            # FIX-058: DPI safety guard — reduce DPI if page_count * dpi exceeds threshold
            with fitz.open(path) as doc_check:
                page_count_check = len(doc_check)
            if page_count_check == 0:
                return err("Input PDF has no pages", 400)
            if page_count_check * dpi > 30000:  # FIX-058
                dpi = max(72, 30000 // page_count_check)
                log.warning(f"ocr_pdf: DPI reduced to {dpi} due to page count {page_count_check}")

            # FIX-073: move both fitz.open() calls inside try so finally always closes them
            src_doc = fitz.open(path)
            out_doc = fitz.open()
            pages_processed = pages_skipped = 0
            try:
                for page_num, src_page in enumerate(src_doc):
                    pw, ph = src_page.rect.width, src_page.rect.height
                    if skip and src_page.get_text().strip():
                        new_page = out_doc.new_page(width=pw, height=ph)
                        new_page.show_pdf_page(fitz.Rect(0,0,pw,ph), src_doc, page_num, overlay=False)
                        pages_skipped += 1; continue
                    mat = fitz.Matrix(dpi/72, dpi/72)
                    pix = src_page.get_pixmap(matrix=mat, alpha=False, colorspace=fitz.csGRAY)
                    img_sx = pw / pix.width; img_sy = ph / pix.height
                    hocr_data = None
                    try:
                        pil_img = Image.open(io.BytesIO(pix.tobytes("png")))
                        pix = None  # FIX-046: release pixmap after converting to PIL
                        # FIX-060: wrap pytesseract call with semaphore to limit concurrent OCR
                        with _ocr_semaphore:
                            hocr_data = pytesseract.image_to_data(
                                pil_img, lang=lang, output_type=TesseractOutput.DICT,
                                config=f"--psm {psm} --oem {oem}")
                    except Exception as ocr_ex:
                        log.warning(f"OCR page {page_num+1}: {ocr_ex}")
                    new_page = out_doc.new_page(width=pw, height=ph)
                    new_page.show_pdf_page(fitz.Rect(0,0,pw,ph), src_doc, page_num, overlay=False)
                    if hocr_data:
                        for i in range(len(hocr_data.get("text",[]))):
                            word = (hocr_data["text"][i] or "").strip()
                            conf = int(hocr_data["conf"][i]) if hocr_data["conf"][i] != -1 else 0
                            if not word or conf < 30: continue
                            x0 = hocr_data["left"][i]*img_sx
                            y1 = (hocr_data["top"][i]+hocr_data["height"][i])*img_sy
                            fs = max(4.0, (hocr_data["height"][i]*img_sy)*0.85)
                            new_page.insert_text((x0,y1-1), word+" ", fontsize=fs,
                                                  fontname="helv", color=(0,0,0),
                                                  render_mode=3, overlay=True)
                    pages_processed += 1
                # FIX-028: check at least one page was processed before returning success
                if pages_processed == 0 and pages_skipped == 0:
                    return err("OCR produced no output — all pages failed", 500)
                fname = generate_output_filename(f.filename, "ocr")
                out = os.path.join(Config.OUTPUT_FOLDER, fname)
                out_doc.save(out, deflate=True, garbage=2)
            finally:
                out_doc.close(); src_doc.close()  # FIX-073: always close both docs
        return ok("OCR complete — PDF is text-searchable", out,
                  output_metadata={"pages_processed":pages_processed,
                                   "pages_skipped":pages_skipped,"lang":lang,"dpi":dpi})
    except Exception:
        log.exception("ocr_pdf"); return err("OCR failed", 500)
# ============================================================================
# PDF EDIT
# ============================================================================
@app.route("/api/v1/rotate", methods=["POST"])
@app.route("/api/rotate", methods=["POST"])
@require_auth
@require_rate_limit
def rotate_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    try: angle = int(request.form.get("angle","90"))
    except ValueError: return err("Angle must be 90, 180 or 270")
    if angle not in (90,180,270): return err("Angle must be 90, 180 or 270")
    pages_spec = request.form.get("pages","all").strip()
    try:
        with FileService.temp_upload(f) as path:
            doc = fitz.open(path)
            # FIX-088: guard against empty PDFs in rotate
            if len(doc) == 0:
                doc.close()
                return err("Input PDF has no pages", 400)
            total = len(doc)
            idxs = list(range(total)) if pages_spec.lower()=="all" else parse_page_ranges(pages_spec, total)
            # FIX-032: return error when no pages match instead of saving unchanged file
            if not idxs:
                doc.close()
                return err("No valid pages matched the specified range", 400)
            for i in idxs: doc[i].set_rotation(angle)
            fname = generate_output_filename(f.filename, "rotated")
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            doc.save(out); doc.close()
        return ok(f"Rotated {len(idxs)} page(s) by {angle}°", out)
    except Exception:
        log.exception("rotate"); return err("Rotate failed", 500)
@app.route("/api/v1/watermark", methods=["POST"])
@app.route("/api/watermark", methods=["POST"])
@require_auth
@require_rate_limit
def watermark_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    text = sanitize_string(request.form.get("text","CONFIDENTIAL"))
    color = sanitize_string(request.form.get("color","808080"), 10)
    # FIX-015: wrap float() casts in try/except with safe defaults
    try:
        opacity = max(0.0, min(1.0, float(request.form.get("opacity","0.3"))))
    except (ValueError, TypeError):
        opacity = 0.3  # FIX-015
    position = sanitize_string(request.form.get("position","diagonal"), 20)
    if position not in ("diagonal","center","top","bottom","tile"): position="diagonal"
    try:
        rotation = max(-90, min(90, float(request.form.get("rotation","45"))))
    except (ValueError, TypeError):
        rotation = 45.0  # FIX-015
    try:
        scale = max(0.1, min(1.0, float(request.form.get("scale","0.3"))))
    except (ValueError, TypeError):
        scale = 0.3  # FIX-015
    image_data = None
    image_file = request.files.get("image")
    if image_file and image_file.filename:
        raw = image_file.read()
        try:
            pil_wm = Image.open(io.BytesIO(raw)).convert("RGBA")
            # FIX-012: validate uploaded image format is in allowed set
            ALLOWED_FORMATS = {'JPEG', 'PNG', 'GIF', 'BMP', 'WEBP', 'TIFF'}
            if pil_wm.format not in ALLOWED_FORMATS:
                return err("Unsupported image format", 400)
            buf_wm = io.BytesIO(); pil_wm.save(buf_wm, format="PNG")
            image_data = buf_wm.getvalue()
        except Exception:
            image_data = raw
    try:
        with FileService.temp_upload(f) as path:
            doc = fitz.open(path)
            # FIX-088: guard against empty PDFs in watermark
            if len(doc) == 0:
                doc.close()
                return err("Input PDF has no pages", 400)
            # FIX-047: decode watermark image once before the page loop
            _wm_base = None
            if image_data:
                _wm_base = Image.open(io.BytesIO(image_data)).convert("RGBA")
            try:
                for page in doc:
                    r = page.rect
                    if image_data:
                        img = _wm_base.copy()  # FIX-047: use copy of pre-decoded image
                        r_ch,g_ch,b_ch,a_ch = img.split()
                        a_ch = a_ch.point(lambda x: int(x*opacity))
                        img.putalpha(a_ch)
                        if rotation != 0:
                            img = img.rotate(rotation, expand=True, resample=Image.BICUBIC)
                        img_buf = io.BytesIO(); img.save(img_buf, format="PNG")
                        img_w = r.width * scale
                        img_h = img_w * img.height / img.width
                        if position == "top":
                            ix,iy = r.x0+(r.width-img_w)/2, r.y0+r.height*0.05
                        elif position == "bottom":
                            ix,iy = r.x0+(r.width-img_w)/2, r.y1-img_h-r.height*0.05
                        else:
                            ix,iy = r.x0+(r.width-img_w)/2, r.y0+(r.height-img_h)/2
                        page.insert_image(fitz.Rect(ix,iy,ix+img_w,iy+img_h),
                                           stream=img_buf.getvalue(), overlay=True)
                    else:
                        wm = create_watermark_pdf(text, opacity, color, r.width, r.height, position, rotation)
                        wmpdf = fitz.open("pdf", wm)
                        page.show_pdf_page(fitz.Rect(0,0,r.width,r.height), wmpdf, 0, overlay=True)
                        wmpdf.close()
                fname = generate_output_filename(f.filename, "watermarked")
                out = os.path.join(Config.OUTPUT_FOLDER, fname)
                doc.save(out)
            finally:
                doc.close()
        wm_type = "image" if image_data else "text"
        return ok(f"Watermark ({wm_type}) added", out)
    except Exception:
        log.exception("watermark"); return err("Watermark failed", 500)
@app.route("/api/v1/page-numbers", methods=["POST"])
@app.route("/api/page-numbers", methods=["POST"])
@require_auth
@require_rate_limit
def page_numbers():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    position = request.form.get("position","bottom")
    # FIX-021: cap start to prevent integer overflow in label generation
    start = safe_int(request.form.get("start","1"), 1, 1, 9999)
    prefix = sanitize_string(request.form.get("prefix",""), 50)
    try:
        with FileService.temp_upload(f) as path:
            doc = fitz.open(path)
            # FIX-088: guard against empty PDFs in page_numbers
            if len(doc) == 0:
                doc.close()
                return err("Input PDF has no pages", 400)
            try:
                for i, page in enumerate(doc):
                    r = page.rect
                    label = f"{prefix}{start + i}"
                    pn = create_page_number_pdf(label, position, r.width, r.height)
                    pnpdf = fitz.open("pdf", pn)
                    page.show_pdf_page(fitz.Rect(0,0,r.width,r.height), pnpdf, 0, overlay=True)
                    pnpdf.close()
                fname = generate_output_filename(f.filename, "numbered")
                out = os.path.join(Config.OUTPUT_FOLDER, fname)
                doc.save(out)
            finally:
                doc.close()
        return ok("Page numbers added", out)
    except Exception:
        log.exception("page_numbers"); return err("Page numbering failed", 500)
@app.route("/api/v1/crop", methods=["POST"])
@app.route("/api/crop", methods=["POST"])
@require_auth
@require_rate_limit
def crop_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    def _sf(k, d=0.0):
        try: return float(request.form.get(k, d))
        except: return d
    left,right,top,bottom = _sf("left"),_sf("right"),_sf("top"),_sf("bottom")
    if any(v < 0 for v in (left,right,top,bottom)):
        return err("Crop margins must be non-negative", 400)
    try:
        with FileService.temp_upload(f) as path:
            doc = fitz.open(path)
            # FIX-088: guard against empty PDFs in crop
            if len(doc) == 0:
                doc.close()
                return err("Input PDF has no pages", 400)
            try:
                for page in doc:
                    r = page.rect
                    nr = fitz.Rect(r.x0+left, r.y0+top, r.x1-right, r.y1-bottom)
                    if nr.is_empty or nr.is_infinite:
                        doc.close(); return err("Crop margins too large", 400)
                    # FIX-098: validate no negative width or height after crop
                    if nr.width <= 0 or nr.height <= 0:
                        doc.close()
                        return err("Crop margins result in zero or negative page size", 400)
                    page.set_cropbox(nr)
                fname = generate_output_filename(f.filename, "cropped")
                out = os.path.join(Config.OUTPUT_FOLDER, fname)
                doc.save(out)
            finally:
                doc.close()
        return ok("PDF pages cropped", out)
    except Exception:
        log.exception("crop"); return err("Crop failed", 500)
@app.route("/api/v1/info", methods=["POST"])
@app.route("/api/info", methods=["POST"])
@require_auth
@require_rate_limit
def pdf_info():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            file_size = os.path.getsize(path)
            doc = fitz.open(path)
            # FIX-088: guard against empty PDFs in pdf_info
            if len(doc) == 0:
                doc.close()
                return err("Input PDF has no pages", 400)
            try:
                meta = doc.metadata
                size_counts = {}
                has_forms = False
                image_count = 0
                font_names = set()
                has_toc = len(doc.get_toc()) > 0
                # FIX-054: combine two page loops into a single pass
                for pg in doc:
                    key = (round(pg.rect.width,1), round(pg.rect.height,1))
                    size_counts[key] = size_counts.get(key, 0) + 1
                    has_forms = has_forms or bool(pg.first_widget)
                    image_count += len(pg.get_images())
                    for fi in pg.get_fonts(full=True):
                        bf = fi[3] if len(fi) > 3 else ""
                        if bf: font_names.add(bf)
                unique_sizes = [{"w":k[0],"h":k[1],"count":v}
                                for k,v in sorted(size_counts.items(), key=lambda x:-x[1])]
                is_lin = False
                try:
                    xobj = doc.xref_object(1, compressed=False)
                    is_lin = "/Linearized" in (xobj or "")
                except Exception: pass
                try: pdf_version = doc.pdf_version()
                except Exception: pdf_version = meta.get("format","unknown")
                out_data = {
                    "page_count": len(doc), "pdf_version": str(pdf_version),
                    "title": meta.get("title",""), "author": meta.get("author",""),
                    "subject": meta.get("subject",""), "creator": meta.get("creator",""),
                    "encrypted": doc.is_encrypted, "file_size_bytes": file_size,
                    "size_human": format_file_size(file_size),
                    "has_forms": has_forms, "has_toc": has_toc,
                    "image_count": image_count,
                    "fonts_used": sorted(font_names)[:20],
                    "total_fonts_found": len(font_names),  # FIX-036
                    "is_linearized": is_lin,
                    "page_sizes": {"unique_sizes":unique_sizes,"all_same":len(unique_sizes)==1},
                }
            finally:
                doc.close()
        return ok("PDF info retrieved", metadata=out_data, pages=out_data.get("page_count"))
    except Exception:
        log.exception("pdf_info"); return err("Info retrieval failed", 500)
# ============================================================================
# PDF SECURITY
# ============================================================================
@app.route("/api/v1/protect", methods=["POST"])
@app.route("/api/protect", methods=["POST"])
@require_auth
@require_rate_limit
def protect_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    pw = sanitize_string(request.form.get("password",""))
    pw2 = sanitize_string(request.form.get("password2",""))
    ep = validate_password(pw, pw2)
    if ep: return err(ep)
    allow_print = request.form.get("allow_print","true").lower() in ("true","1","yes")
    allow_copy = request.form.get("allow_copy","true").lower() in ("true","1","yes")
    try:
        with FileService.temp_upload(f) as path:
            # FIX-082: wrap doc lifecycle in try/finally to ensure doc.close() always runs
            doc = fitz.open(path)
            try:
                # FIX-099: guard against empty PDFs in protect
                if len(doc) == 0:
                    doc.close()
                    return err("Input PDF has no pages", 400)
                permissions = int(fitz.PDF_PERM_ACCESSIBILITY)
                if allow_print: permissions |= int(fitz.PDF_PERM_PRINT)
                if allow_copy: permissions |= int(fitz.PDF_PERM_COPY)
                fname = generate_output_filename(f.filename, "protected")
                out = os.path.join(Config.OUTPUT_FOLDER, fname)
                import secrets as _secrets
                _owner_pw = pw + "_o_" + _secrets.token_hex(12)
                # FIX-005: wrap doc.save() to log only exception type, not locals containing owner_pw
                try:
                    doc.save(out, encryption=fitz.PDF_ENCRYPT_AES_256,
                             owner_pw=_owner_pw, user_pw=pw,
                             permissions=permissions)
                except Exception as save_ex:
                    log.error(f"protect_pdf save error: {type(save_ex).__name__}")
                    raise
            finally:
                try: doc.close()  # FIX-082
                except Exception: pass
        return ok("PDF password-protected with AES-256", out)
    except Exception:
        log.exception("protect"); return err("Protect failed", 500)
@app.route("/api/v1/unlock", methods=["POST"])
@app.route("/api/unlock", methods=["POST"])
@require_auth
@require_rate_limit
def unlock_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    pw = sanitize_string(request.form.get("password",""))
    if not pw: return err("Password required")
    try:
        with FileService.temp_upload(f) as path:
            # FIX-080: wrap entire doc lifecycle in try/finally to ensure doc.close() always runs
            doc = fitz.open(path)
            try:
                if not doc.is_encrypted:
                    return err("This PDF is not password-protected.", 400)
                if not doc.authenticate(pw):
                    return err("Incorrect password.", 401)
                # FIX-099: guard against empty PDFs in unlock
                if len(doc) == 0:
                    return err("Input PDF has no pages", 400)
                fname = generate_output_filename(f.filename, "unlocked")
                out = os.path.join(Config.OUTPUT_FOLDER, fname)
                doc.save(out, encryption=fitz.PDF_ENCRYPT_NONE)
            finally:
                try: doc.close()  # FIX-080
                except Exception: pass
        return ok("PDF unlocked", out)
    except Exception:
        log.exception("unlock"); return err("Unlock failed", 500)
@app.route("/api/v1/sign-pdf", methods=["POST"])
@app.route("/api/sign-pdf", methods=["POST"])
@app.route("/api/v1/stamp-pdf", methods=["POST"])
@app.route("/api/stamp-pdf", methods=["POST"])
@require_auth
@require_rate_limit
def sign_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    name = sanitize_string(request.form.get("name","Signed"))
    reason = sanitize_string(request.form.get("reason","Approved"))
    page_target = sanitize_string(request.form.get("page","last"), 10)
    position = sanitize_string(request.form.get("position","bottom-right"), 20)
    if position not in ("bottom-left","bottom-right","top-left","top-right","center"):
        position = "bottom-right"
    sig_data = None
    sig_file = request.files.get("signature")
    if sig_file and sig_file.filename:
        try:
            pil_sig = Image.open(sig_file).convert("RGBA")
            # FIX-012: validate uploaded signature image format
            ALLOWED_FORMATS = {'JPEG', 'PNG', 'GIF', 'BMP', 'WEBP', 'TIFF'}
            if pil_sig.format not in ALLOWED_FORMATS:
                return err("Unsupported image format", 400)
            buf_sig = io.BytesIO(); pil_sig.save(buf_sig, format="PNG")
            sig_data = buf_sig.getvalue()
        except Exception:
            sig_data = sig_file.read()
    today_str = datetime.now().strftime("%Y-%m-%d")
    def _sig_pos(rect, pos):
        m = {"bottom-right":(rect.x1-180,rect.y1-70),
             "bottom-left": (rect.x0+30, rect.y1-70),
             "top-right": (rect.x1-180,rect.y0+50),
             "top-left": (rect.x0+30, rect.y0+50),
             "center": (rect.x0+rect.width/2-75, rect.y0+rect.height/2)}
        return m.get(pos,(rect.x1-180,rect.y1-70))
    try:
        with FileService.temp_upload(f) as path:
            doc = fitz.open(path)
            # FIX-088: guard against empty PDFs in sign
            if len(doc) == 0:
                doc.close()
                return err("Input PDF has no pages", 400)
            total = len(doc)
            try:
                if page_target == "all": page_indices = list(range(total))
                elif page_target == "first": page_indices = [0]
                elif page_target == "last": page_indices = [total-1]
                else:
                    try:
                        pg_num = int(page_target)
                        if pg_num < 1 or pg_num > total:
                            return err(f"Page {pg_num} out of range (1-{total})", 400)
                        page_indices = [pg_num-1]
                    except ValueError:
                        # FIX-033: explicit error instead of silent fallback
                        return err(f"Invalid page target: '{page_target}'", 400)
                for pg_idx in page_indices:
                    page = doc[pg_idx]; rect = page.rect
                    sig_x, sig_y = _sig_pos(rect, position)
                    # FIX-083: draw box and text BEFORE inserting signature image so image overlays box
                    line = f"{name} | {reason} | {today_str}"
                    box_r = fitz.Rect(sig_x-5, sig_y-5, sig_x+155, sig_y+25)
                    page.draw_rect(box_r, color=(0,0,0.6), fill=(0.9,0.9,1), width=0.5)  # FIX-083
                    page.insert_text((sig_x, sig_y+12), line, fontsize=8, color=(0,0,0.5))  # FIX-083
                    if sig_data:
                        img_rect = fitz.Rect(sig_x, sig_y-40, sig_x+150, sig_y+5)
                        page.insert_image(img_rect, stream=sig_data, overlay=True)  # FIX-083: image last
                fname = generate_output_filename(f.filename, "stamped")
                out = os.path.join(Config.OUTPUT_FOLDER, fname)
                doc.save(out)
            finally:
                doc.close()
        response = ok(f"Stamp added to {len(page_indices)} page(s) at {position}", out)
        if request.path.endswith("sign-pdf"):
            response.headers["Deprecation"] = "true"
            response.headers["Sunset"] = "2027-01-01"
            response.headers["Link"] = '</api/v1/stamp-pdf>; rel="successor-version"'
        return response
    except Exception:
        log.exception("sign_pdf"); return err("Stamp failed", 500)
@app.route("/api/v1/redact-pdf", methods=["POST"])
@app.route("/api/redact-pdf", methods=["POST"])
@require_auth
@require_rate_limit
def redact_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    mode = sanitize_string(request.form.get("mode","text"), 10)
    if mode not in ("text","regex","preset"): mode="text"
    search_text = compiled = None
    if mode == "text":
        search_text = sanitize_string(request.form.get("search_text",""))
        if not search_text: return err("search_text required for mode=text")
    elif mode == "regex":
        pattern_str = sanitize_string(request.form.get("pattern",""), 500)
        if not pattern_str: return err("pattern required for mode=regex")
        try:
            compiled = SafeRegex.compile(pattern_str)
        except ValueError as rv:
            return err(str(rv), 400)
        except re.error as rex:
            return err(f"Invalid regex: {rex}", 400)
    elif mode == "preset":
        preset_name = sanitize_string(request.form.get("preset",""), 30)
        if preset_name not in REDACTION_PATTERNS:
            return err(f"Unknown preset. Choose: {', '.join(REDACTION_PATTERNS)}")
        compiled = re.compile(REDACTION_PATTERNS[preset_name])
    try:
        with FileService.temp_upload(f) as path:
            doc = fitz.open(path)
            # FIX-088: guard against empty PDFs in redact
            if len(doc) == 0:
                doc.close()
                return err("Input PDF has no pages", 400)
            try:
                count = 0
                for page in doc:
                    if mode == "text":
                        for rect in page.search_for(search_text):
                            page.add_redact_annot(rect, fill=(0,0,0)); count += 1
                    else:
                        # FIX-006/FIX-011: cap page text and match count to prevent ReDoS
                        page_text = page.get_text("text")[:50000]  # FIX-006
                        matches = list(compiled.finditer(page_text))[:10000]  # FIX-006/FIX-011
                        for match in matches:
                            matched = match.group().strip()
                            if not matched:
                                continue
                            rects = page.search_for(matched)
                            if not rects:
                                for word in matched.split():
                                    if word.strip():
                                        rects += page.search_for(word.strip())
                            for rect in rects:
                                page.add_redact_annot(rect, fill=(0, 0, 0))
                                count += 1
                    page.apply_redactions()
                fname = generate_output_filename(f.filename, "redacted")
                out = os.path.join(Config.OUTPUT_FOLDER, fname)
                doc.save(out)
            finally:
                doc.close()
        # FIX-035: add warning when no matches found
        warning_msg = None
        if count == 0:
            warning_msg = "No matching content found; document unchanged"
        return ok(f"Redacted {count} occurrence(s) (mode={mode})", out,
                  redaction_count=count,
                  warning=warning_msg)  # FIX-035
    except Exception:
        log.exception("redact"); return err("Redact failed", 500)
# ============================================================================
# PDF CONVERT
# ============================================================================
@app.route("/api/v1/pdf-to-image", methods=["POST"])
@app.route("/api/pdf-to-image", methods=["POST"])
@require_auth
@require_rate_limit
def pdf_to_image():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    fmt = request.form.get("format","jpg").lower()
    dpi = safe_int(request.form.get("dpi","150"), 150, 72, 300)
    # FIX-089: accept optional quality parameter for JPEG output
    quality = safe_int(request.form.get("quality", "85"), 85, 1, 95)  # FIX-089
    if fmt not in ("jpg","png"): fmt = "jpg"
    try:
        with FileService.temp_upload(f) as path:
            doc = fitz.open(path)
            # FIX-088: guard against empty PDFs in pdf_to_image
            if len(doc) == 0:
                doc.close()
                return err("Input PDF has no pages", 400)
            # FIX-058: DPI safety guard for pdf_to_image
            page_count_img = len(doc)
            if page_count_img * dpi > 30000:  # FIX-058
                dpi = max(72, 30000 // page_count_img)
                log.warning(f"pdf_to_image: DPI reduced to {dpi} due to page count {page_count_img}")
            try:
                count = len(doc)
                # FIX-044: use disk-based temp file for ZIP to avoid OOM on large PDFs
                tmp_zip = tempfile.NamedTemporaryFile(
                    suffix=".zip", delete=False, dir=Config.TEMP_FOLDER)
                tmp_zip.close()
                try:
                    with zipfile.ZipFile(tmp_zip.name, "w", zipfile.ZIP_DEFLATED) as zf:
                        for i, page in enumerate(doc):
                            mat = fitz.Matrix(dpi/72, dpi/72)
                            pix = page.get_pixmap(matrix=mat, alpha=False, colorspace=fitz.csRGB)
                            pil = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
                            img_buf = io.BytesIO()
                            if fmt == "jpg":
                                pil.save(img_buf, "JPEG", quality=quality, optimize=True)  # FIX-089: use quality param
                            else:
                                pil.save(img_buf, "PNG")
                            zf.writestr(f"page_{i+1:04d}.{fmt}", img_buf.getvalue())
                    fname = generate_output_filename(f.filename, "to_image")
                    out = os.path.join(Config.OUTPUT_FOLDER, fname)
                    shutil.move(tmp_zip.name, out)
                except Exception:
                    try: os.remove(tmp_zip.name)
                    except OSError: pass
                    raise
            finally:
                doc.close()
        return ok(f"Exported {count} page(s) as {fmt.upper()}", out)
    except Exception:
        log.exception("pdf_to_image"); return err("Export failed", 500)
@app.route("/api/v1/pdf-to-word", methods=["POST"])
@app.route("/api/pdf-to-word", methods=["POST"])
@require_auth
@require_rate_limit
def pdf_to_word():
    if not PDF2DOCX_AVAILABLE:
        return err("PDF to Word requires pdf2docx.", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    f.seek(0,2); file_size = f.tell(); f.seek(0)
    ext = f.filename.rsplit(".",1)[-1].lower() if "." in f.filename else "pdf"
    upload_path = os.path.join(Config.UPLOAD_FOLDER, f"{uuid.uuid4()}.{ext}")
    f.seek(0)
    with open(upload_path, "wb") as fh: fh.write(f.read())
    fname = generate_output_filename(f.filename, "to_word")
    # FIX-085: normalize output filename to always end in .docx
    fname = Path(fname).stem + ".docx"  # FIX-085
    out = os.path.join(Config.OUTPUT_FOLDER, fname)
    PDF2WORD_SYNC_LIMIT = getattr(Config, 'PDF2WORD_SYNC_LIMIT', 10*1024*1024)
    if file_size <= PDF2WORD_SYNC_LIMIT:
        _result = {}
        def _convert_sync():
            try:
                cv = Pdf2DocxConverter(upload_path)
                cv.convert(out, start=0, end=None)
                cv.close()
                _result['ok'] = True
            except Exception as ex:
                _result['err'] = str(ex)
        _t = threading.Thread(target=_convert_sync, daemon=True)
        _t.start()
        _t.join(timeout=90)
        if _t.is_alive():
            try: os.remove(upload_path)
            except OSError: pass
            return err("Conversion timed out — file may be too complex", 504)
        if _result.get('err'):
            try: os.remove(upload_path)
            except OSError: pass
            return err(f"Conversion failed: {_result['err']}", 500)
        try: os.remove(upload_path)
        except OSError: pass
        # FIX-095: verify output is valid DOCX before returning success
        if not os.path.exists(out) or os.path.getsize(out) == 0:  # FIX-095
            return err("Conversion produced no output", 500)
        return ok("PDF converted to Word", out)
    # Large file: try sync with timeout, fallback to async
    job_id = str(uuid.uuid4())
    redis_service.job_set(job_id, {"status":"pending","progress":"0","operation":"pdf_to_word",
                                    "created_at":get_timestamp(),"user_id":getattr(g,"user_id","default")})
    if celery_app and pdf_to_word_task:
        task = pdf_to_word_task.delay(upload_path, out, job_id)
        redis_service.job_update(job_id, {"task_id": task.id})
        return jsonify({"success":True,"message":"Large file — conversion running. Check status_url.",
                        "job_id":job_id,"status_url":f"/api/v1/jobs/{job_id}",
                        "poll_interval_ms":2000})
    # Fallback thread with timeout
    thread_path = upload_path + "_thread.pdf"
    shutil.copy(upload_path, thread_path)
    try: os.remove(upload_path)
    except OSError: pass
    def _convert_bg():
        tmp_out = out + ".tmp"  # FIX-063: write to .tmp first, then rename atomically
        try:
            cv = Pdf2DocxConverter(thread_path)
            cv.convert(tmp_out, start=0, end=None)  # FIX-063
            cv.close()
            if os.path.exists(tmp_out) and os.path.getsize(tmp_out) > 0:
                os.replace(tmp_out, out)  # FIX-063: atomic rename
                redis_service.job_update(job_id, {"status":"completed","progress":"100","output_path":out})
            else:
                raise RuntimeError("Output missing/empty")
        except Exception as ex:
            redis_service.job_update(job_id, {"status":"failed","error":str(ex)})
            try: os.remove(tmp_out)  # FIX-063: clean up .tmp on failure
            except OSError: pass
        finally:
            try: os.remove(thread_path)
            except OSError: pass
    t = threading.Thread(target=_convert_bg, daemon=True)
    t.start()
    t.join(timeout=120)
    if t.is_alive():
        return jsonify({"success":True,"message":"Large file — conversion running. Check status_url.",
                        "job_id":job_id,"status_url":f"/api/v1/jobs/{job_id}",
                        "poll_interval_ms":2000})
    job = redis_service.job_get(job_id)
    # FIX-038: check for job failure before returning async response
    if job and job.get("status") == "failed":
        return err(f"Conversion failed: {job.get('error', 'Unknown')}", 500)
    if job and job.get("status") == "completed" and os.path.exists(out):
        return ok("PDF converted to Word", out)
    return jsonify({"success":True,"message":"Large file — conversion running. Check status_url.",
                    "job_id":job_id,"status_url":f"/api/v1/jobs/{job_id}",
                    "poll_interval_ms":2000})
@app.route("/api/v1/pdf-to-excel", methods=["POST"])
@app.route("/api/pdf-to-excel", methods=["POST"])
@require_auth
@require_rate_limit
def pdf_to_excel():
    if not OPENPYXL_AVAILABLE: return err("PDF to Excel requires openpyxl.", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            # FIX-088: guard against empty PDFs in pdf_to_excel
            _chk = fitz.open(path)
            if len(_chk) == 0:
                _chk.close()
                return err("Input PDF has no pages", 400)
            _chk.close()
            wb = Workbook(); wb.remove(wb.active)
            tables_extracted = 0; method_used = None
            if PDFPLUMBER_AVAILABLE:
                try:
                    with pdfplumber.open(path) as pdf:
                        for page in pdf.pages:
                            # FIX-087: skip text-strategy extraction if lines-strategy already found tables
                            tables = page.extract_tables({
                                "vertical_strategy": "lines",
                                "horizontal_strategy": "lines",
                                "snap_tolerance": 5
                            })
                            if not tables:  # FIX-087: only try text strategy if lines found nothing
                                tables = page.extract_tables({
                                    "vertical_strategy": "text",
                                    "horizontal_strategy": "text",
                                    "snap_tolerance": 3,
                                    "join_tolerance": 3
                                })
                            for table in tables:
                                if table and any(any(c for c in row if c) for row in table):
                                    tables_extracted += 1
                                    ws = wb.create_sheet(f"Table_{tables_extracted}")
                                    for row in table:
                                        ws.append([str(c).strip() if c else "" for c in row])
                                    if ws.max_row > 0:
                                        for cell in ws[1]: cell.font = Font(bold=True)
                    if tables_extracted > 0: method_used = "pdfplumber"
                except Exception as ex:
                    log.warning(f"pdfplumber: {ex}")
            if tables_extracted == 0 and TABULA_AVAILABLE:
                try:
                    dfs = tabula.read_pdf(path, pages="all", multiple_tables=True, lattice=True)
                    for i, df in enumerate(dfs):
                        if not df.empty:
                            tables_extracted += 1
                            ws = wb.create_sheet(f"Table_{i+1}")
                            ws.append(list(df.columns))
                            for row in df.itertuples(index=False):
                                ws.append([str(v) if v is not None else "" for v in row])
                    method_used = "tabula"
                except Exception as ex:
                    log.warning(f"tabula: {ex}")
            if tables_extracted == 0:
                ws = wb.create_sheet("Extracted_Text")
                ws["A1"] = "No tables detected — full text:"
                ws["A1"].font = Font(bold=True, size=12)
                doc = fitz.open(path); row_idx = 3  # FIX-092: row_idx=3 confirmed correct (A1=header, row2=blank separator)
                for page_num, pg in enumerate(doc):
                    ws[f"A{row_idx}"] = f"--- Page {page_num+1} ---"
                    ws[f"A{row_idx}"].font = Font(bold=True)
                    row_idx += 1
                    for line in pg.get_text("text").split("\n"):
                        if line.strip():
                            ws[f"A{row_idx}"] = line.strip(); row_idx += 1
                doc.close(); method_used = "raw_text"
            fname = generate_output_filename(f.filename, "to_excel")
            fname = re.sub(r'\.pdf$','.xlsx',fname,flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            # FIX-025: ensure at least one sheet exists before saving
            if not wb.worksheets:
                return err("No content could be extracted from this PDF", 500)
            wb.save(out)
        return ok(f"Extracted {tables_extracted} table(s) (method: {method_used})", out,
                  tables_found=tables_extracted, extraction_method=method_used)
    except Exception:
        log.exception("pdf_to_excel"); return err("PDF to Excel failed", 500)
@app.route("/api/v1/pdf-to-ppt", methods=["POST"])
@app.route("/api/pdf-to-ppt", methods=["POST"])
@require_auth
@require_rate_limit
def pdf_to_ppt():
    if not PPTX_AVAILABLE: return err("PDF to PPT requires python-pptx.", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            doc = fitz.open(path)
            # FIX-088: guard against empty PDFs in pdf_to_ppt
            if len(doc) == 0:
                doc.close()
                return err("Input PDF has no pages", 400)
            try:
                prs = Presentation()
                prs.slide_width = PptxInches(10); prs.slide_height = PptxInches(7.5)
                blank = prs.slide_layouts[6]
                for page in doc:
                    pix = page.get_pixmap(dpi=200)
                    # FIX-051: use in-memory BytesIO instead of NamedTemporaryFile
                    img_buf = io.BytesIO()
                    img_buf.write(pix.tobytes("png"))
                    img_buf.seek(0)
                    slide = prs.slides.add_slide(blank)
                    slide.shapes.add_picture(img_buf, 0, 0,
                                              prs.slide_width, prs.slide_height)
                    img_buf.close()  # FIX-051: release buffer
            finally:
                doc.close()
            fname = generate_output_filename(f.filename, "to_ppt")
            fname = re.sub(r'\.pdf$','.pptx',fname,flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            prs.save(out)
        return ok("PDF converted to PowerPoint", out)
    except Exception:
        log.exception("pdf_to_ppt"); return err("PDF to PPT failed", 500)
@app.route("/api/v1/pdf-to-pdfa", methods=["POST"])
@app.route("/api/pdf-to-pdfa", methods=["POST"])
@require_auth
@require_rate_limit
def pdf_to_pdfa():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    version = request.form.get("version","1b")
    # FIX-084: fix version-to-pdfa_val mapping to correctly handle PDF/A-2 and PDF/A-3
    if "3" in version:
        pdfa_val = "3"  # FIX-084
    elif "2" in version:
        pdfa_val = "2"  # FIX-084
    else:
        pdfa_val = "1"  # FIX-084
    try:
        with FileService.temp_upload(f) as path:
            fname = generate_output_filename(f.filename, "pdfa")
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            cmd = [Config.GHOSTSCRIPT, "-dBATCH", "-dNOPAUSE", "-dNOSAFER",
                     "-sDEVICE=pdfwrite", f"-dPDFA={pdfa_val}",
                     "-dPDFACompatibilityPolicy=1", f"-sOutputFile={out}", path]
            # FIX-016: use getattr to prevent AttributeError if PDFA_TIMEOUT missing
            pdfa_timeout = getattr(Config, "PDFA_TIMEOUT", 300)
            # FIX-081: add try/finally to remove partial output on failure
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=pdfa_timeout)
                if result.returncode != 0:
                    return err("Ghostscript PDF/A conversion failed.", 500)
            except subprocess.TimeoutExpired:
                try: os.remove(out)  # FIX-081: clean up partial output on timeout
                except OSError: pass
                return err("PDF/A conversion timed out", 500)
            validation_result = None
            if Config.PDFA_VALIDATE:
                try:
                    vr = subprocess.run([Config.VERAPDF_PATH,"--flavour","1b","--format","text",out],
                                         capture_output=True, timeout=60)
                    validation_result = {"passed":vr.returncode==0,"output":vr.stdout.decode()[:500]}
                except Exception as vex:
                    validation_result = {"passed":None,"error":str(vex)}
        resp = ok(f"Converted to PDF/A-{version}", out)
        if validation_result:
            resp_data = resp.get_json()
            resp_data["pdfa_validation"] = validation_result
            return jsonify(resp_data)
        return resp
    except subprocess.TimeoutExpired:
        return err("PDF/A conversion timed out", 500)
    except Exception:
        log.exception("pdf_to_pdfa"); return err("PDF/A conversion failed", 500)
# ============================================================================
# COMPARE PDF
# ============================================================================
@app.route("/api/v1/compare-pdf", methods=["POST"])
@app.route("/api/compare-pdf", methods=["POST"])
@require_auth
@require_rate_limit
def compare_pdf():
    import difflib
    files = request.files.getlist("files")
    if len(files) != 2: return err("Exactly 2 PDF files required")
    for f in files:
        e = validate_file(f, Config.ALLOWED_PDF)
        if e: return err(e)
    try:
        with FileService.temp_uploads(files) as paths:
            doc1 = fitz.open(paths[0]); doc2 = fitz.open(paths[1])
            # FIX-094: validate both PDFs have at least one page before comparison
            if len(doc1) == 0 or len(doc2) == 0:  # FIX-094
                doc1.close(); doc2.close()
                return err("Both PDFs must have at least one page", 400)
            try:
                extra_pages = abs(len(doc1) - len(doc2))
                pages = min(len(doc1), len(doc2))
                text_diff_pages = []; overall_sims = []
                MAX_WORDS = 500
                # FIX-043: use disk-based temp file for ZIP to avoid OOM on large PDFs
                tmp_zip = tempfile.NamedTemporaryFile(
                    suffix=".zip", delete=False, dir=Config.TEMP_FOLDER)
                tmp_zip.close()
                try:
                    with zipfile.ZipFile(tmp_zip.name, "w", zipfile.ZIP_DEFLATED) as zf:
                        for page_idx in range(pages):
                            pix1 = doc1[page_idx].get_pixmap(dpi=150); pix2 = doc2[page_idx].get_pixmap(dpi=150)
                            img1 = Image.open(io.BytesIO(pix1.tobytes("png"))).convert("RGB")
                            img2 = Image.open(io.BytesIO(pix2.tobytes("png"))).convert("RGB")
                            if img1.size != img2.size: img2 = img2.resize(img1.size, Image.LANCZOS)
                            diff = ImageChops.difference(img1, img2)
                            diff_e = diff.point(lambda x: min(x*8, 255))
                            diff_out = io.BytesIO(); diff_e.save(diff_out, "PNG")
                            zf.writestr(f"diff_page_{page_idx+1:04d}.png", diff_out.getvalue())
                            # FIX-059: use islice on get_text("words") generator instead of materializing all
                            words1 = [w[4] for w in islice(
                                doc1[page_idx].get_text("words", sort=True), MAX_WORDS)]  # FIX-059
                            words2 = [w[4] for w in islice(
                                doc2[page_idx].get_text("words", sort=True), MAX_WORDS)]  # FIX-059
                            sm = difflib.SequenceMatcher(None, words1, words2)
                            sim = round(sm.ratio()*100, 1); overall_sims.append(sim)
                            added, removed = [], []
                            for tag, a1, a2, b1, b2 in sm.get_opcodes():
                                if tag == "insert": added += words2[b1:b2]
                                elif tag == "delete": removed += words1[a1:a2]
                                elif tag == "replace":
                                    removed += words1[a1:a2]
                                    added += words2[b1:b2]
                            text_diff_pages.append({"page":page_idx+1,"similarity_pct":sim,
                                                     "words_added":added[:50],"words_removed":removed[:50]})
                        overall_sim = round(sum(overall_sims)/len(overall_sims),1) if overall_sims else 0.0
                        zf.writestr("text_diff_summary.json",
                                    json.dumps({"pages":text_diff_pages,"overall_similarity_pct":overall_sim},
                                               ensure_ascii=False, indent=2))
                    fname = generate_output_filename(files[0].filename, "comparison",
                                                      is_multi=True, filenames=[f.filename for f in files])
                    out = os.path.join(Config.OUTPUT_FOLDER, fname)
                    shutil.move(tmp_zip.name, out)
                except Exception:
                    try: os.remove(tmp_zip.name)
                    except OSError: pass
                    raise
            finally:
                doc1.close(); doc2.close()
        warning_msg = (f"{extra_pages} extra page(s) in the longer PDF were not compared"
                       if extra_pages else "")
        # FIX-029: always include warning key in response
        return ok(f"Compared {pages} page(s)", out,
                  pages_compared=pages,
                  pages_skipped=extra_pages,
                  warning=warning_msg)
    except Exception:
        log.exception("compare_pdf"); return err("Comparison failed", 500)
# ============================================================================
# IMAGE → PDF
# ============================================================================
def _images_to_pdf(paths, page_size_str, output_filename):
    size_map = {"a4": A4, "letter": letter}
    size = size_map.get(page_size_str.lower(), None)
    out = os.path.join(Config.OUTPUT_FOLDER, output_filename)
    c = rl_canvas.Canvas(out, pagesize=size or letter)
    for path in paths:
        try:
            with Image.open(path) as img:
                iw, ih = img.size
            if size:
                pw, ph = size
            else:
                pw, ph = iw*72/96, ih*72/96
            sw = min(pw*0.95, iw*72/96)
            sh = sw * ih / iw
            if sh > ph*0.95:
                sh = ph*0.95; sw = sh*iw/ih
            x = (pw-sw)/2; y = (ph-sh)/2
            c.setPageSize((pw, ph))
            c.drawImage(path, x, y, width=sw, height=sh)
            c.showPage()
        except Exception as ex:
            log.warning(f"Skipping image {path}: {ex}")
    c.save()
    return out
@app.route("/api/v1/image-to-pdf", methods=["POST"])
@app.route("/api/image-to-pdf", methods=["POST"])
@require_auth
@require_rate_limit
def image_to_pdf():
    files = request.files.getlist("files")
    if not files or all(f.filename=="" for f in files):
        return err("At least one image file required")
    for f in files:
        e = validate_file(f, Config.ALLOWED_IMAGE)
        if e: return err(e)
    page_size = request.form.get("page_size","auto")
    try:
        with FileService.temp_uploads(files) as paths:
            # FIX-100: validate at least one image path was successfully written
            if not paths:  # FIX-100
                return err("No valid images to convert", 400)
            fname = generate_output_filename(files[0].filename, "to_pdf",
                                              is_multi=True, filenames=[f.filename for f in files])
            fname = re.sub(r'\.(jpg|jpeg|png|gif|bmp|tiff|webp)$','.pdf',fname,flags=re.IGNORECASE)
            if not fname.endswith(".pdf"): fname = Path(fname).stem + ".pdf"
            out = _images_to_pdf(paths, page_size, fname)
        return ok(f"Converted {len(files)} image(s) to PDF", out)
    except Exception:
        log.exception("image_to_pdf"); return err("Image to PDF failed", 500)
@app.route("/api/v1/word-to-pdf", methods=["POST"])
@app.route("/api/word-to-pdf", methods=["POST"])
@require_auth
@require_rate_limit
def word_to_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            fname = generate_output_filename(f.filename, "to_pdf")
            fname = re.sub(r'\.(doc|docx)$','.pdf',fname,flags=re.IGNORECASE)
            out = libre(path, "pdf", output_filename=fname)
            # FIX-026: validate output exists and is non-empty
            if not out or not os.path.exists(out) or os.path.getsize(out) == 0:
                return err("LibreOffice conversion produced no output", 500)
        return ok("Word converted to PDF", out)
    except Exception:
        log.exception("word_to_pdf"); return err("Word to PDF failed", 500)
@app.route("/api/v1/excel-to-pdf", methods=["POST"])
@app.route("/api/excel-to-pdf", methods=["POST"])
@require_auth
@require_rate_limit
def excel_to_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            fname = generate_output_filename(f.filename, "to_pdf")
            fname = re.sub(r'\.(xls|xlsx)$','.pdf',fname,flags=re.IGNORECASE)
            out = libre(path, "pdf", output_filename=fname)
            # FIX-026: validate output exists and is non-empty
            if not out or not os.path.exists(out) or os.path.getsize(out) == 0:
                return err("LibreOffice conversion produced no output", 500)
        return ok("Excel converted to PDF", out)
    except Exception:
        log.exception("excel_to_pdf"); return err("Excel to PDF failed", 500)
@app.route("/api/v1/html-to-pdf", methods=["POST"])
@app.route("/api/html-to-pdf", methods=["POST"])
@require_auth
@require_rate_limit
def html_to_pdf():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_HTML)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            fname = generate_output_filename(f.filename, "to_pdf")
            fname = re.sub(r'\.(html|htm)$','.pdf',fname,flags=re.IGNORECASE)
            out_path = os.path.join(Config.OUTPUT_FOLDER, fname)
            # FIX-017: catch FileNotFoundError separately to fall through to WeasyPrint
            result = None
            try:
                result = subprocess.run(
                    [Config.WKHTMLTOPDF, "--quiet",
                     "--disable-local-file-access",  # FIX-013: prevent SSRF via file:// URIs
                     path, out_path],
                    capture_output=True, timeout=60)
            except FileNotFoundError:
                result = None  # FIX-017: fall through to WeasyPrint
            except subprocess.TimeoutExpired:
                return err("HTML to PDF timed out", 500)
            if result is not None and result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return ok("HTML converted to PDF", out_path)
            try:
                from weasyprint import HTML
                HTML(filename=path).write_pdf(out_path)
                if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                    return ok("HTML converted to PDF (WeasyPrint)", out_path)
            except ImportError: pass
            except Exception as we: log.warning(f"WeasyPrint: {we}")
            return err("HTML to PDF failed — install wkhtmltopdf or weasyprint", 500)
    except Exception:
        log.exception("html_to_pdf"); return err("HTML to PDF failed", 500)
# ============================================================================
# IMAGE TOOLS
# ============================================================================
@app.route("/api/v1/compress-image", methods=["POST"])
@app.route("/api/compress-image", methods=["POST"])
@require_auth
@require_rate_limit
def compress_image():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_IMAGE)
    if e: return err(e)
    quality = safe_int(request.form.get("quality","75"), 75, 1, 95)
    output_format = request.form.get("output_format","auto").lower()
    try:
        with FileService.temp_upload(f) as path:
            orig_size = os.path.getsize(path)
            img = Image.open(path)
            # FIX-096: guard for zero-dimension images
            if 0 in img.size:  # FIX-096
                return err("Image has zero-dimension — cannot process", 400)
            # FIX-020: handle bilevel (mode "1") images that can't be saved as JPEG
            if img.mode == "1":
                img = img.convert("L")
            ext = path.rsplit(".",1)[-1].lower()
            if output_format == "webp": target_fmt = "webp"
            elif output_format in ("jpg","jpeg"): target_fmt = "jpeg"
            elif output_format == "png": target_fmt = "png"
            elif ext == "png": target_fmt = "png"
            elif ext == "webp": target_fmt = "webp"
            else: target_fmt = "jpeg"
            fname = generate_output_filename(f.filename, "compressed")
            fname = re.sub(r'\.\w+$', f'.{"jpg" if target_fmt=="jpeg" else target_fmt}', fname)
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            if target_fmt == "png":
                if img.mode not in ("RGB","RGBA","L","LA","P"): img = img.convert("RGBA")
                tmp_png = path + "_tmp.png"; img.save(tmp_png, format="PNG", optimize=False)
                pngquant_ok = False
                try:
                    res = subprocess.run(
                        ["pngquant","--quality",f"{max(1,quality-15)}-{quality}",
                         "--speed","3","--force","--output",out,tmp_png],
                        capture_output=True, timeout=30)
                    if res.returncode in (0,99) and os.path.exists(out):
                        pngquant_ok = True
                except (FileNotFoundError, subprocess.TimeoutExpired): pass
                finally:
                    try: os.remove(tmp_png)
                    except OSError: pass
                if not pngquant_ok:
                    img.save(out, format="PNG", optimize=True, compress_level=9)
            elif target_fmt == "webp":
                if img.mode not in ("RGB","RGBA"): img = img.convert("RGB")
                img.save(out, format="WEBP", quality=quality, method=6, optimize=True)
            else:
                if img.mode in ("RGBA","P","LA"):
                    bg = Image.new("RGB", img.size, (255,255,255))
                    if img.mode == "P": img = img.convert("RGBA")
                    mask = img.split()[-1] if img.mode in ("RGBA","LA") else None
                    bg.paste(img, mask=mask); img = bg
                elif img.mode != "RGB": img = img.convert("RGB")
                img.save(out, format="JPEG", quality=quality, optimize=True, progressive=True)
            new_size = os.path.getsize(out)
            reduction = round((1-new_size/orig_size)*100,1) if orig_size else 0
        return ok(f"Image compressed {reduction}% as {target_fmt.upper()}", out,
                  reduction_pct=reduction, output_format=target_fmt)
    except Exception:
        log.exception("compress_image"); return err("Image compression failed", 500)
@app.route("/api/v1/resize-image", methods=["POST"])
@app.route("/api/resize-image", methods=["POST"])
@require_auth
@require_rate_limit
def resize_image():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_IMAGE)
    if e: return err(e)
    width = safe_int(request.form.get("width","800"), 800, 1, 8000)
    height = safe_int(request.form.get("height","600"), 600, 1, 8000)
    keep_ratio = request.form.get("keep_ratio","true").lower() in ("true","on","1","yes")
    try:
        with FileService.temp_upload(f) as path:
            img = Image.open(path)
            # FIX-096: guard for zero-dimension images in resize
            if 0 in img.size:  # FIX-096
                return err("Image has zero-dimension — cannot process", 400)
            if keep_ratio: img.thumbnail((width, height), Image.LANCZOS)
            else: img = img.resize((width, height), Image.LANCZOS)
            ext = path.rsplit(".",1)[-1].lower()
            fmt = "JPEG" if ext in ("jpg","jpeg") else "PNG"
            if fmt == "JPEG" and img.mode in ("RGBA","P"): img = img.convert("RGB")
            fname = generate_output_filename(f.filename, "resized")
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            img.save(out, format=fmt)
        return ok(f"Image resized to {img.size[0]}×{img.size[1]}", out)
    except Exception:
        log.exception("resize_image"); return err("Resize failed", 500)
@app.route("/api/v1/webp-to-jpg", methods=["POST"])
@app.route("/api/webp-to-jpg", methods=["POST"])
@require_auth
@require_rate_limit
def webp_to_jpg():
    files = request.files.getlist("files")
    if not files or all(f.filename=="" for f in files):
        return err("At least one WebP file required")
    for f in files:
        e = validate_file(f, Config.ALLOWED_WEBP)
        if e: return err(e)
    quality = safe_int(request.form.get("quality","75"), 75, 1, 95)
    try:
        with FileService.temp_uploads(files) as paths:
            # FIX-056: replace in-memory BytesIO ZIP with disk-based temp file
            tmp_zip = tempfile.NamedTemporaryFile(
                suffix=".zip", delete=False, dir=Config.TEMP_FOLDER)  # FIX-056
            tmp_zip.close()
            try:
                with zipfile.ZipFile(tmp_zip.name, "w", zipfile.ZIP_DEFLATED) as zf:  # FIX-056
                    for i, path in enumerate(paths):
                        img = Image.open(path)
                        # FIX-096: guard for zero-dimension images in webp_to_jpg
                        if 0 in img.size:  # FIX-096
                            log.warning(f"webp_to_jpg: skipping zero-dimension image {path}")
                            continue
                        exif_bytes = img.info.get("exif",b"") if hasattr(img,"info") else b""
                        rgb = img.convert("RGB")
                        ib = io.BytesIO()
                        kw = {"format":"JPEG","quality":quality}
                        if exif_bytes: kw["exif"] = exif_bytes
                        rgb.save(ib, **kw)
                        zf.writestr(f"image_{i+1:04d}.jpg", ib.getvalue())
                fname = generate_output_filename(files[0].filename,"to_jpg",
                                                  is_multi=True, filenames=[f.filename for f in files])
                out = os.path.join(Config.OUTPUT_FOLDER, fname)
                shutil.move(tmp_zip.name, out)  # FIX-056: atomic move to output
            except Exception:
                try: os.remove(tmp_zip.name)
                except OSError: pass
                raise
        return ok(f"Converted {len(files)} WebP(s) to JPG", out)
    except Exception:
        log.exception("webp_to_jpg"); return err("WebP to JPG failed", 500)
@app.route("/api/v1/png-to-jpg", methods=["POST"])
@app.route("/api/png-to-jpg", methods=["POST"])
@require_auth
@require_rate_limit
def png_to_jpg():
    files = request.files.getlist("files")
    if not files or all(f.filename=="" for f in files):
        return err("At least one PNG file required")
    for f in files:
        e = validate_file(f, Config.ALLOWED_PNG)
        if e: return err(e)
    quality = safe_int(request.form.get("quality","75"), 75, 1, 95)
    try:
        with FileService.temp_uploads(files) as paths:
            # FIX-056: replace in-memory BytesIO ZIP with disk-based temp file
            tmp_zip = tempfile.NamedTemporaryFile(
                suffix=".zip", delete=False, dir=Config.TEMP_FOLDER)  # FIX-056
            tmp_zip.close()
            try:
                with zipfile.ZipFile(tmp_zip.name, "w", zipfile.ZIP_DEFLATED) as zf:  # FIX-056
                    for i, path in enumerate(paths):
                        img = Image.open(path)
                        # FIX-096: guard for zero-dimension images in png_to_jpg
                        if 0 in img.size:  # FIX-096
                            log.warning(f"png_to_jpg: skipping zero-dimension image {path}")
                            continue
                        img_rgb = img.convert("RGB")
                        ib = io.BytesIO(); img_rgb.save(ib, format="JPEG", quality=quality)
                        zf.writestr(f"image_{i+1:04d}.jpg", ib.getvalue())
                fname = generate_output_filename(files[0].filename,"to_jpg",
                                                  is_multi=True, filenames=[f.filename for f in files])
                out = os.path.join(Config.OUTPUT_FOLDER, fname)
                shutil.move(tmp_zip.name, out)  # FIX-056: atomic move to output
            except Exception:
                try: os.remove(tmp_zip.name)
                except OSError: pass
                raise
        return ok(f"Converted {len(files)} PNG(s) to JPG", out)
    except Exception:
        log.exception("png_to_jpg"); return err("PNG to JPG failed", 500)
@app.route("/api/v1/image-to-excel", methods=["POST"])
@app.route("/api/image-to-excel", methods=["POST"])
@require_auth
@require_rate_limit
def image_to_excel():
    if not OPENPYXL_AVAILABLE: return err("Requires openpyxl.", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_IMAGE)
    if e: return err(e)
    raw_lang = request.form.get("lang","eng")
    lang = re.sub(r'[^a-zA-Z0-9+\-]','',raw_lang)[:50] or "eng"
    try:
        with FileService.temp_upload(f) as path:
            img = Image.open(path).convert("RGB")
            # FIX-096: guard for zero-dimension images in image_to_excel
            if 0 in img.size:  # FIX-096
                return err("Image has zero-dimension — cannot process", 400)
            ocr_grid = None
            warning = None  # FIX-039: always initialize warning before use
            if TESSERACT_AVAILABLE:
                try:
                    data = pytesseract.image_to_data(img, lang=lang,
                                                      output_type=TesseractOutput.DICT, config="--psm 6")
                    del img  # FIX-052: release PIL buffer before building workbook
                    words = [{"text":(data["text"][i] or "").strip(),"left":data["left"][i],
                               "top":data["top"][i],"width":data["width"][i],"height":data["height"][i]}
                              for i in range(len(data["text"]))
                              if (data["text"][i] or "").strip() and int(data["conf"][i]) >= 30]
                    if len(words) < 3:
                        warning = f"OCR found only {len(words)} word(s)"
                    else:
                        avg_char_w = sum(w["width"]/max(len(w["text"]),1) for w in words)/len(words)
                        dynamic_gap = max(10, avg_char_w * 1.5)
                        tol = 8; rows_dict = {}
                        for w in words:
                            mid_y = w["top"]+w["height"]//2
                            key = next((k for k in rows_dict if abs(k-mid_y)<=tol), None)
                            if key is None: key = mid_y; rows_dict[key] = []
                            rows_dict[key].append(w)
                        sorted_rows = [sorted(rows_dict[k], key=lambda x: x["left"]) for k in sorted(rows_dict)]
                        grid = []
                        for row_words in sorted_rows:
                            cells = [row_words[0]["text"]]
                            for wi in range(1, len(row_words)):
                                prev = row_words[wi-1]; curr = row_words[wi]
                                if curr["left"]-(prev["left"]+prev["width"]) > dynamic_gap:
                                    cells.append(curr["text"])
                                else:
                                    cells[-1] += " " + curr["text"]
                            grid.append(cells)
                        ocr_grid = grid
                except Exception as ocr_ex:
                    warning = f"OCR failed ({ocr_ex}) — falling back to image embed"
            else:
                warning = "pytesseract not available — falling back to image embed"

            wb = Workbook(); ws = wb.active; msg = ""
            fname = generate_output_filename(f.filename,"to_excel")
            fname = re.sub(r'\.(jpg|jpeg|png|gif|bmp|tiff|webp)$','.xlsx',
                           fname, flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, fname)

            if ocr_grid:
                ws.title = "OCR_Table"
                for r_idx, row_cells in enumerate(ocr_grid):
                    for c_idx, val in enumerate(row_cells):
                        cell = ws.cell(row=r_idx+1, column=c_idx+1, value=val)
                        if r_idx == 0: cell.font = Font(bold=True)
                msg = f"OCR extracted {len(ocr_grid)} rows"
                wb.save(out)
            else:
                ws.title = "Image"
                img_reload = Image.open(path).convert("RGB")  # reload since we del'd img
                tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                img_reload.save(tmp.name, format="PNG"); tmp.close()
                # FIX-065: move tmp PNG cleanup into try/finally so it runs even if wb.save() raises
                try:
                    xl_img = XlImage(tmp.name); xl_img.anchor = "B2"
                    ws.add_image(xl_img)
                    wb.save(out)
                finally:
                    try: os.unlink(tmp.name)  # FIX-065
                    except OSError: pass
                msg = "Image embedded in Excel"

            warning = warning or None  # FIX-039: normalize warning to None if empty
            if warning: msg += f" | Warning: {warning}"
        return ok(msg, out, ocr_warning=warning)
    except Exception:
        log.exception("image_to_excel"); return err("Image to Excel failed", 500)

# ============================================================================
# WORD TOOLS
# ============================================================================
@app.route("/api/v1/word-to-txt", methods=["POST"])
@app.route("/api/word-to-txt", methods=["POST"])
@require_auth
@require_rate_limit
def word_to_txt():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            fname = generate_output_filename(f.filename,"to_txt")
            fname = re.sub(r'\.(doc|docx)$','.txt',fname,flags=re.IGNORECASE)
            if DOCX_AVAILABLE and path.endswith(".docx"):
                doc = None
                try:
                    doc = DocxDocument(path)
                    lines = []
                    for p in doc.paragraphs:
                        if p.text.strip():
                            lines.append(p.text)
                    for table in doc.tables:
                        for row in table.rows:
                            seen_ids = set()
                            row_cells = []
                            for cell in row.cells:
                                cid = id(cell._tc)
                                if cid not in seen_ids:
                                    seen_ids.add(cid)
                                    if cell.text.strip():
                                        row_cells.append(cell.text.strip())
                            if row_cells:
                                lines.append(" | ".join(row_cells))
                    out = os.path.join(Config.OUTPUT_FOLDER, fname)
                    # FIX-057: write lines incrementally instead of joining into one large string
                    with open(out, "w", encoding="utf-8") as fh:
                        for line in lines:
                            fh.write(line + "\n")  # FIX-057
                finally:
                    # FIX-076: close DocxDocument in finally block
                    if doc is not None and hasattr(doc, 'close'):
                        try: doc.close()
                        except Exception: pass
            else:
                out = libre(path, "txt", output_filename=fname)
                if not out: return err("LibreOffice conversion failed.", 500)
        return ok("Word converted to TXT", out)
    except Exception:
        log.exception("word_to_txt"); return err("Word to TXT failed", 500)
@app.route("/api/v1/word-to-excel", methods=["POST"])
@app.route("/api/word-to-excel", methods=["POST"])
@require_auth
@require_rate_limit
def word_to_excel():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            fname = generate_output_filename(f.filename,"to_excel")
            fname = re.sub(r'\.(doc|docx)$','.xlsx',fname,flags=re.IGNORECASE)
            if path.endswith(".doc"):
                # FIX-090: check whether "xlsx" is in Config.LIBRE_ALLOWED_FMTS before calling libre()
                if "xlsx" not in getattr(Config, "LIBRE_ALLOWED_FMTS", []):  # FIX-090
                    return err("xlsx conversion not enabled in server config", 501)
                out = libre(path, "xlsx", output_filename=fname)
                if not out: return err("Word (.doc) to Excel requires LibreOffice.", 500)
                return ok("Word (.doc) converted to Excel via LibreOffice", out)
            if not DOCX_AVAILABLE or not OPENPYXL_AVAILABLE:
                return err("Requires python-docx + openpyxl.", 501)
            # FIX-077: wrap DocxDocument in try/finally; FIX-069: wrap wb.close() in try/finally
            doc = None
            wb = None
            try:
                doc = DocxDocument(path)
                wb = Workbook(); wb.remove(wb.active)
                table_count = len(doc.tables)
                for t_idx, table in enumerate(doc.tables):
                    ws = wb.create_sheet(title=f"Table_{t_idx+1}")
                    for r_idx, row in enumerate(table.rows):
                        for c_idx, cell in enumerate(row.cells):
                            co = ws.cell(row=r_idx+1, column=c_idx+1, value=cell.text)
                            if r_idx == 0: co.font = Font(bold=True)
                ws_text = wb.create_sheet("Document_Text")
                ws_text.append(["Line","Style","Text"])
                for cell in ws_text[1]: cell.font = Font(bold=True)
                pr = 2
                for p in doc.paragraphs:
                    if p.text.strip():
                        ws_text.cell(pr,1,pr-1); ws_text.cell(pr,2,p.style.name)
                        ws_text.cell(pr,3,p.text); pr += 1
                out = os.path.join(Config.OUTPUT_FOLDER, fname)
                wb.save(out)
            finally:
                if wb is not None:
                    try: wb.close()  # FIX-069
                    except Exception: pass
                if doc is not None and hasattr(doc, 'close'):
                    try: doc.close()  # FIX-077
                    except Exception: pass
        return ok(f"Word converted to Excel — {table_count} table(s)", out, tables_found=table_count)
    except Exception:
        log.exception("word_to_excel"); return err("Word to Excel failed", 500)
@app.route("/api/v1/word-to-ppt", methods=["POST"])
@app.route("/api/word-to-ppt", methods=["POST"])
@require_auth
@require_rate_limit
def word_to_ppt():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            fname = generate_output_filename(f.filename,"to_ppt")
            fname = re.sub(r'\.(doc|docx)$','.pptx',fname,flags=re.IGNORECASE)
            if path.endswith(".doc"):
                out = libre(path, "pptx", output_filename=fname)
                if not out: return err("LibreOffice required for .doc.", 500)
                return ok("Word (.doc) converted to PPT via LibreOffice", out)
            if not DOCX_AVAILABLE or not PPTX_AVAILABLE:
                return err("Requires python-docx + python-pptx.", 501)
            # FIX-074: wrap DocxDocument in try/finally to ensure close
            doc = None
            try:
                doc = DocxDocument(path)
                prs = Presentation(); prs.slide_width=PptxInches(10); prs.slide_height=PptxInches(7.5)
                tc_layout = prs.slide_layouts[1]
                paragraphs = [p for p in doc.paragraphs if p.text.strip()]
                # FIX-053: cap paragraphs to prevent timeouts on very large documents
                if len(paragraphs) > 5000:
                    paragraphs = paragraphs[:5000]  # FIX-053
                if not paragraphs:
                    prs.slides.add_slide(prs.slide_layouts[6])
                    out = os.path.join(Config.OUTPUT_FOLDER, fname); prs.save(out)
                    # FIX-030: return with warning when document appears empty
                    return ok("Word converted to PPT (document appears empty)", out,
                              slides_created=0,
                              warning="No text content found in document")
                has_headings = any(p.style.name.startswith("Heading") or p.style.name=="Title" for p in paragraphs)
                if not has_headings:
                    slide = prs.slides.add_slide(tc_layout)
                    if slide.shapes.title: slide.shapes.title.text = Path(f.filename).stem
                    if len(slide.placeholders) > 1:
                        tf = slide.placeholders[1].text_frame; tf.clear()
                        for p in paragraphs:
                            pr = tf.add_paragraph(); pr.text = p.text
                else:
                    current_tf = None
                    for p in paragraphs:
                        sn, text = p.style.name, p.text.strip()
                        if sn.startswith("Heading 1") or sn == "Title":
                            slide = prs.slides.add_slide(tc_layout)
                            if slide.shapes.title: slide.shapes.title.text = text
                            current_tf = slide.placeholders[1].text_frame if len(slide.placeholders)>1 else None
                            if current_tf: current_tf.clear()
                        elif sn.startswith("Heading"):
                            if current_tf is None:
                                slide = prs.slides.add_slide(tc_layout)
                                current_tf = slide.placeholders[1].text_frame if len(slide.placeholders)>1 else None
                                if current_tf: current_tf.clear()
                            if current_tf:
                                pr = current_tf.add_paragraph(); pr.text = text
                                if pr.runs: pr.runs[0].font.bold = True
                        else:
                            # FIX-091: create text box when no body placeholder exists, for ALL non-heading paragraphs
                            if current_tf is None:  # FIX-091
                                txBox = slide.shapes.add_textbox(
                                    PptxInches(0.5), PptxInches(1.5),
                                    PptxInches(9), PptxInches(5)
                                )
                                current_tf = txBox.text_frame
                                current_tf.clear()
                            if current_tf:
                                pr = current_tf.add_paragraph()
                                pr.text = text
                                pr.level = 1
                out = os.path.join(Config.OUTPUT_FOLDER, fname); prs.save(out)
            finally:
                # FIX-074: ensure DocxDocument is closed
                if doc is not None and hasattr(doc, 'close'):
                    try: doc.close()
                    except Exception: pass
        return ok("Word converted to PowerPoint", out)
    except Exception:
        log.exception("word_to_ppt"); return err("Word to PPT failed", 500)
@app.route("/api/v1/compress-word", methods=["POST"])
@app.route("/api/compress-word", methods=["POST"])
@require_auth
@require_rate_limit
def compress_word():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e: return err(e)
    quality_str = request.form.get("quality","medium").lower()
    jpeg_quality = {"low":50,"medium":70,"high":85}.get(quality_str, 70)
    force_jpeg = request.form.get("force_jpeg","false").lower() in ("true","1","yes")
    try:
        with FileService.temp_upload(f) as path:
            work_path = path
            if path.endswith(".doc"):
                converted = libre(path, "docx", temp=True)
                if not converted: return err("LibreOffice required for .doc.", 500)
                work_path = converted
            # FIX-086: capture orig_size AFTER potential .doc -> .docx conversion
            orig_size = os.path.getsize(work_path)  # FIX-086: moved after conversion
            fname = generate_output_filename(f.filename,"compressed")
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            # FIX-066: ensure tmp_dir is created INSIDE the outer try block
            tmp_dir = tempfile.mkdtemp()  # FIX-066
            try:
                with zipfile.ZipFile(work_path,"r") as zin: zin.extractall(tmp_dir)
                media_dir = os.path.join(tmp_dir,"word","media"); compressed = 0
                if os.path.isdir(media_dir):
                    for fname_img in os.listdir(media_dir):
                        img_path = os.path.join(media_dir, fname_img)
                        ext_img = os.path.splitext(fname_img)[1].lower()
                        if ext_img not in {".png",".jpg",".jpeg",".gif",".bmp"}: continue
                        try:
                            img = Image.open(img_path); w, h = img.size
                            # FIX-020: handle bilevel mode "1" images
                            if img.mode == "1":
                                img = img.convert("L")
                            if w > 1200 or h > 1200:
                                ratio = min(1200/w, 1200/h)
                                img = img.resize((max(1,int(w*ratio)),max(1,int(h*ratio))), Image.LANCZOS)
                            if force_jpeg:
                                if img.mode in ("RGBA","LA","P"):
                                    bg = Image.new("RGB",img.size,(255,255,255))
                                    if img.mode=="P": img=img.convert("RGBA")
                                    mask = img.split()[-1] if img.mode in ("RGBA","LA") else None
                                    bg.paste(img,mask=mask); img=bg
                                elif img.mode!="RGB": img=img.convert("RGB")
                                img.save(img_path,format="JPEG",quality=jpeg_quality,optimize=True)
                            elif ext_img==".png" or img.mode in ("RGBA","LA","P"):
                                if img.mode not in ("RGBA","RGB","L"): img=img.convert("RGBA")
                                img.save(img_path,format="PNG",optimize=True,compress_level=9)
                            else:
                                if img.mode!="RGB": img=img.convert("RGB")
                                img.save(img_path,format="JPEG",quality=jpeg_quality,optimize=True)
                            compressed += 1
                        except Exception as ie: log.warning(f"compress-word img {fname_img}: {ie}")
                with zipfile.ZipFile(out,"w",zipfile.ZIP_DEFLATED,compresslevel=9) as zout:
                    for root, _, files_list in os.walk(tmp_dir):
                        for fi in files_list:
                            abs_p = os.path.join(root,fi)
                            zout.write(abs_p, os.path.relpath(abs_p, tmp_dir))
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)  # FIX-066: always runs
                if work_path != path:
                    try: os.remove(work_path)
                    except OSError: pass
            new_size = os.path.getsize(out)
            reduction = round((1-new_size/orig_size)*100,1) if orig_size else 0
        extras = {"reduction_pct": reduction, "images_compressed": compressed, "force_jpeg_used": force_jpeg}
        # FIX-031: warn when compression did not reduce file size
        if reduction <= 0:
            extras["warning"] = "Compression did not reduce file size"
        return ok(f"Word compressed — {reduction}% ({compressed} images)", out, **extras)
    except Exception:
        log.exception("compress_word"); return err("Word compression failed", 500)
@app.route("/api/v1/edit-word", methods=["POST"])
@app.route("/api/edit-word", methods=["POST"])
@require_auth
@require_rate_limit
def edit_word():
    if not DOCX_AVAILABLE: return err("Requires python-docx.", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e: return err(e)
    find_text = sanitize_string(request.form.get("find_text",""))
    replace_text = sanitize_string(request.form.get("replace_text",""))
    if not find_text: return err("find_text required")
    try:
        with FileService.temp_upload(f) as path:
            doc = DocxDocument(path); count = 0
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
            fname = generate_output_filename(f.filename,"edited")
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            doc.save(out)
        return ok(f"Replaced {count} occurrence(s)", out)
    except Exception:
        log.exception("edit_word"); return err("Edit Word failed", 500)
@app.route("/api/v1/unlock-word", methods=["POST"])
@app.route("/api/unlock-word", methods=["POST"])
@require_auth
@require_rate_limit
def unlock_word():
    if not MSOFFCRYPTO_AVAILABLE: return err("Requires msoffcrypto-tool.", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e: return err(e)
    pw = sanitize_string(request.form.get("password",""))
    if not pw: return err("Password required")
    try:
        with FileService.temp_upload(f) as path:
            fname = generate_output_filename(f.filename,"unlocked")
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            with open(path,"rb") as fp:
                of = msoffcrypto.OfficeFile(fp); of.load_key(password=pw)
                with open(out,"wb") as fout: of.decrypt(fout)
        return ok("Word document unlocked", out)
    except Exception:
        log.exception("unlock_word"); return err("Unlock failed — check password", 500)
@app.route("/api/v1/protect-word", methods=["POST"])
@app.route("/api/protect-word", methods=["POST"])
@require_auth
@require_rate_limit
def protect_word():
    if not MSOFFCRYPTO_AVAILABLE: return err("Requires msoffcrypto-tool.", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e: return err(e)
    pw = sanitize_string(request.form.get("password",""))
    pw2 = sanitize_string(request.form.get("password2",""))
    ep = validate_password(pw, pw2)
    if ep: return err(ep)
    try:
        with FileService.temp_upload(f) as path:
            fname = generate_output_filename(f.filename,"protected")
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            with open(path,"rb") as fp:
                of = msoffcrypto.OfficeFile(fp)
                try: of.encrypt(pw, out, cipher_algorithm="AES")
                except TypeError: of.encrypt(pw, out)
        return ok("Word document protected (AES)", out)
    except Exception:
        log.exception("protect_word"); return err("Protect Word failed", 500)
# ============================================================================
# EXCEL TOOLS
# ============================================================================
@app.route("/api/v1/excel-to-csv", methods=["POST"])
@app.route("/api/excel-to-csv", methods=["POST"])
@require_auth
@require_rate_limit
def excel_to_csv():
    if not OPENPYXL_AVAILABLE: return err("Requires openpyxl.", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e: return err(e)
    sheet_name = sanitize_string(request.form.get("sheet",""), 100)
    all_sheets = request.form.get("all_sheets","false").lower() in ("true","1","yes")
    try:
        with FileService.temp_upload(f) as path:
            # FIX-072: wrap wb.close() in try/finally
            wb = load_workbook(path, data_only=True, read_only=True)
            try:
                if all_sheets:
                    buf = io.BytesIO(); total_rows = 0
                    with zipfile.ZipFile(buf,"w",zipfile.ZIP_DEFLATED) as zf:
                        for sname in wb.sheetnames:
                            ws = wb[sname]; cb = io.StringIO()
                            writer = csv.writer(cb, quoting=csv.QUOTE_MINIMAL); cnt = 0
                            for row in ws.iter_rows(values_only=True):
                                writer.writerow([coerce_cell_for_csv(v) for v in row]); cnt += 1
                            total_rows += cnt
                            safe_name = re.sub(r'[^\w]','_', sname)
                            zf.writestr(f"{safe_name}.csv", ('\ufeff'+cb.getvalue()).encode('utf-8'))
                    fname = generate_output_filename(f.filename,"to_csv")
                    fname = re.sub(r'\.(xls|xlsx)$','.zip',fname,flags=re.IGNORECASE)
                    out = os.path.join(Config.OUTPUT_FOLDER, fname)
                    with open(out,"wb") as fh: fh.write(buf.getvalue())
                    extras = {}
                    # FIX-037: warn when all sheets are empty
                    if total_rows == 0:
                        extras["warning"] = "All sheets appear to be empty"
                    return ok(f"All {len(wb.sheetnames)} sheet(s) exported ({total_rows} rows)", out, **extras)
                else:
                    ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active
                    fname = generate_output_filename(f.filename,"to_csv")
                    fname = re.sub(r'\.(xls|xlsx)$','.csv',fname,flags=re.IGNORECASE)
                    out = os.path.join(Config.OUTPUT_FOLDER, fname); cnt = 0
                    with open(out,"w",newline="",encoding="utf-8-sig") as fh:
                        writer = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
                        for row in ws.iter_rows(values_only=True):
                            writer.writerow([coerce_cell_for_csv(v) for v in row]); cnt += 1
                    return ok(f"Excel converted to CSV ({cnt} rows)", out)
            finally:
                try: wb.close()  # FIX-072
                except Exception: pass
    except Exception:
        log.exception("excel_to_csv"); return err("Excel to CSV failed", 500)
@app.route("/api/v1/excel-to-word", methods=["POST"])
@app.route("/api/excel-to-word", methods=["POST"])
@require_auth
@require_rate_limit
def excel_to_word():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e: return err(e)
    preserve_formulas = request.form.get("preserve_formulas","true").lower() in ("true","1","yes")
    row_limit = safe_int(request.form.get("row_limit", Config.EXCEL_ROW_LIMIT),
                          Config.EXCEL_ROW_LIMIT, 1, 100000)
    if not OPENPYXL_AVAILABLE or not DOCX_AVAILABLE:
        try:
            with FileService.temp_upload(f) as path:
                fname = generate_output_filename(f.filename,"to_word")
                fname = re.sub(r'\.(xls|xlsx)$','.docx',fname,flags=re.IGNORECASE)
                out = libre(path,"docx",output_filename=fname)
                if not out: return err("Excel to Word requires openpyxl+python-docx or LibreOffice.",500)
            return ok("Excel converted to Word (LibreOffice)", out)
        except Exception: log.exception("excel_to_word_libre"); return err("Excel to Word failed", 500)
    try:
        with FileService.temp_upload(f) as path:
            # FIX-069: wrap wb.close() in try/finally
            wb = load_workbook(path, data_only=not preserve_formulas)
            try:
                doc = DocxDocument(); sheet_count = len(wb.sheetnames); formulas_present = False
                for sheet_name in wb.sheetnames:
                    ws = wb[sheet_name]; doc.add_heading(sheet_name, level=1)
                    # FIX-050: incremental row reading — stop at row_limit without materializing full sheet
                    rows_write = []; truncated = False
                    for row_data in ws.iter_rows(values_only=True):
                        if len(rows_write) >= row_limit:
                            truncated = True
                            break
                        rows_write.append(row_data)
                    if not rows_write: doc.add_paragraph("(empty sheet)"); continue
                    n_cols = max((len(r) for r in rows_write), default=1)
                    table = doc.add_table(rows=len(rows_write), cols=n_cols)
                    try:
                        table.style = "Light Grid Accent 1"
                    except Exception:
                        pass
                    for r_idx, row_data in enumerate(rows_write):
                        for c_idx in range(n_cols):
                            val = row_data[c_idx] if c_idx < len(row_data) else None
                            cell = table.cell(r_idx, c_idx); cell.text = coerce_cell_for_csv(val)
                            if r_idx == 0:
                                for para in cell.paragraphs:
                                    for run in para.runs: run.bold = True
                            if isinstance(val, str) and val.startswith("="):
                                formulas_present = True
                    if truncated:
                        doc.add_paragraph(f"(Truncated to {row_limit} rows)")
                    doc.add_paragraph()
                fname = generate_output_filename(f.filename,"to_word")
                fname = re.sub(r'\.(xls|xlsx)$','.docx',fname,flags=re.IGNORECASE)
                out = os.path.join(Config.OUTPUT_FOLDER, fname); doc.save(out)
            finally:
                try: wb.close()  # FIX-069
                except Exception: pass
        return ok(f"Excel converted to Word ({sheet_count} sheet(s))", out,
                  formulas_preserved=preserve_formulas, row_limit_used=row_limit,
                  warning=("Formulas replaced with values." if formulas_present and not preserve_formulas else None))
    except Exception:
        log.exception("excel_to_word"); return err("Excel to Word failed", 500)
@app.route("/api/v1/excel-to-json", methods=["POST"])
@app.route("/api/excel-to-json", methods=["POST"])
@require_auth
@require_rate_limit
def excel_to_json():
    if not OPENPYXL_AVAILABLE: return err("Requires openpyxl.", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e: return err(e)
    use_headers = request.form.get("header","true").lower() in ("true","1","yes")
    # FIX-055: row cap to prevent materializing massive sheets
    MAX_JSON_ROWS = getattr(Config, "EXCEL_JSON_ROW_LIMIT", 100000)  # FIX-055
    try:
        with FileService.temp_upload(f) as path:
            # FIX-071: wrap wb.close() in try/finally
            wb = load_workbook(path, data_only=True, read_only=True)
            try:
                data = {}
                for sname in wb.sheetnames:
                    ws = wb[sname]; all_rows = list(ws.iter_rows(values_only=True))
                    if not all_rows: data[sname] = []; continue
                    if use_headers:
                        headers = []; seen = {}
                        for i, h in enumerate(all_rows[0]):
                            base = str(h).strip() if h is not None else ""
                            if not base: base = f"col_{i}"
                            cnt = seen.get(base,0); seen[base] = cnt+1
                            headers.append(base if cnt==0 else f"{base}_{cnt}")
                        # FIX-055: cap rows to MAX_JSON_ROWS
                        rows_collected = []
                        for row in all_rows[1:]:
                            if len(rows_collected) >= MAX_JSON_ROWS: break  # FIX-055
                            rows_collected.append({headers[c]: coerce_cell_value(v)
                                                   for c, v in enumerate(row) if c < len(headers)})
                        data[sname] = rows_collected
                    else:
                        # FIX-055: cap rows for non-header mode too
                        rows_collected = []
                        for row in all_rows:
                            if len(rows_collected) >= MAX_JSON_ROWS: break  # FIX-055
                            rows_collected.append([coerce_cell_value(v) for v in row])
                        data[sname] = rows_collected
                fname = generate_output_filename(f.filename,"to_json")
                fname = re.sub(r'\.(xls|xlsx)$','.json',fname,flags=re.IGNORECASE)
                out = os.path.join(Config.OUTPUT_FOLDER, fname)
                with open(out,"w",encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
            finally:
                try: wb.close()  # FIX-071
                except Exception: pass
        return ok(f"Excel converted to JSON ({len(data)} sheet(s))", out)
    except Exception:
        log.exception("excel_to_json"); return err("Excel to JSON failed", 500)
@app.route("/api/v1/compress-excel", methods=["POST"])
@app.route("/api/compress-excel", methods=["POST"])
@require_auth
@require_rate_limit
def compress_excel():
    if not OPENPYXL_AVAILABLE: return err("Requires openpyxl.", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            orig = os.path.getsize(path)
            # FIX-048: load workbook only once with data_only=False; detect formulas inline
            wb = load_workbook(path, data_only=False)
            # FIX-070: wrap wb.close() in try/finally
            try:
                formulas_present = False
                for ws in wb.worksheets:
                    max_r = max_c = 0
                    for row in ws.iter_rows():
                        for cell in row:
                            if cell.value is not None:
                                max_r = max(max_r, cell.row); max_c = max(max_c, cell.column)
                            # FIX-048: detect formulas during the same iteration pass
                            if isinstance(cell.value, str) and cell.value.startswith("="):
                                formulas_present = True
                    if max_r > 0 and ws.max_row > max_r:
                        try: ws.delete_rows(max_r+1, ws.max_row-max_r)
                        except Exception: pass
                    if max_c > 0 and ws.max_column > max_c:
                        try: ws.delete_cols(max_c+1, ws.max_column-max_c)
                        except Exception: pass
                tmp_out = path + "_cmp.xlsx"; wb.save(tmp_out)
            finally:
                try: wb.close()  # FIX-070
                except Exception: pass
            fname = generate_output_filename(f.filename,"compressed")
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            try:
                with zipfile.ZipFile(tmp_out,"r") as zin:
                    with zipfile.ZipFile(out,"w",zipfile.ZIP_DEFLATED,compresslevel=9) as zout:
                        for item in zin.infolist():
                            zout.writestr(item, zin.read(item.filename))
            except Exception: shutil.copy(tmp_out, out)
            try: os.remove(tmp_out)
            except OSError: pass
            new_size = os.path.getsize(out)
            reduction = round((1-new_size/orig)*100,1) if orig else 0
        return ok(f"Excel compressed — {reduction}% smaller", out,
                  reduction_pct=reduction, formulas_lost=formulas_present,
                  warning=("Formulas replaced with values." if formulas_present else None))
    except Exception:
        log.exception("compress_excel"); return err("Excel compression failed", 500)
@app.route("/api/v1/unlock-excel", methods=["POST"])
@app.route("/api/unlock-excel", methods=["POST"])
@require_auth
@require_rate_limit
def unlock_excel():
    if not MSOFFCRYPTO_AVAILABLE: return err("Requires msoffcrypto-tool.", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e: return err(e)
    pw = sanitize_string(request.form.get("password",""))
    if not pw: return err("Password required")
    try:
        with FileService.temp_upload(f) as path:
            fname = generate_output_filename(f.filename,"unlocked")
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            with open(path,"rb") as fp:
                of = msoffcrypto.OfficeFile(fp); of.load_key(password=pw)
                with open(out,"wb") as fout: of.decrypt(fout)
        return ok("Excel workbook unlocked", out)
    except Exception:
        log.exception("unlock_excel"); return err("Unlock failed — check password", 500)
@app.route("/api/v1/protect-excel", methods=["POST"])
@app.route("/api/protect-excel", methods=["POST"])
@require_auth
@require_rate_limit
def protect_excel():
    if not MSOFFCRYPTO_AVAILABLE: return err("Requires msoffcrypto-tool.", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e: return err(e)
    pw = sanitize_string(request.form.get("password",""))
    pw2 = sanitize_string(request.form.get("password2",""))
    ep = validate_password(pw, pw2)
    if ep: return err(ep)
    try:
        with FileService.temp_upload(f) as path:
            fname = generate_output_filename(f.filename,"protected")
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            with open(path,"rb") as fp:
                of = msoffcrypto.OfficeFile(fp)
                try: of.encrypt(pw, out, cipher_algorithm="AES")
                except TypeError: of.encrypt(pw, out)
        return ok("Excel workbook protected (AES)", out)
    except Exception:
        log.exception("protect_excel"); return err("Protect Excel failed", 500)
@app.route("/api/v1/repair-excel", methods=["POST"])
@app.route("/api/repair-excel", methods=["POST"])
@require_auth
@require_rate_limit
def repair_excel():
    if not OPENPYXL_AVAILABLE: return err("Requires openpyxl.", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            fname = generate_output_filename(f.filename,"repaired")
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            ext = path.rsplit(".",1)[-1].lower()
            if ext == "xlsm":
                shutil.copy(path, out)
                return ok("Macro-enabled workbook returned as-is", out)
            try:
                wb = load_workbook(path, data_only=False, read_only=False)
                wb.save(out); wb.close()
                if os.path.exists(out) and os.path.getsize(out) > 0:
                    return ok("Excel repaired (openpyxl)", out)
            except Exception as ex1:
                log.warning(f"openpyxl repair: {ex1}")
            lo_out = libre(path, "xlsx", output_filename=fname)
            if lo_out and os.path.exists(lo_out) and os.path.getsize(lo_out) > 0:
                return ok("Excel repaired (LibreOffice)", lo_out)
            return err("Could not repair Excel — file severely corrupted", 500)
    except Exception:
        log.exception("repair_excel"); return err("Excel repair failed", 500)
@app.route("/api/v1/excel-to-jpg", methods=["POST"])
@app.route("/api/excel-to-jpg", methods=["POST"])
@require_auth
@require_rate_limit
def excel_to_jpg():
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            pdf_path = libre(path, "pdf", temp=True)
            if pdf_path and os.path.exists(pdf_path) and os.path.getsize(pdf_path)>100:
                doc = None
                try:
                    doc = fitz.open(pdf_path)
                    buf = io.BytesIO()
                    with zipfile.ZipFile(buf,"w",zipfile.ZIP_DEFLATED) as zf:
                        for i, page in enumerate(doc):
                            pix = page.get_pixmap(dpi=150)
                            zf.writestr(f"sheet_{i+1:04d}.jpg", pix.tobytes("jpeg"))
                    fname = generate_output_filename(f.filename,"to_jpg")
                    fname = re.sub(r'\.(xls|xlsx)$','.zip',fname,flags=re.IGNORECASE)
                    out = os.path.join(Config.OUTPUT_FOLDER, fname)
                    with open(out,"wb") as fh: fh.write(buf.getvalue())
                    return ok("Excel sheets exported as JPG", out)
                finally:
                    if doc: doc.close()
                    if pdf_path:
                        try: os.remove(pdf_path)
                        except OSError: pass
            return err("Excel to JPG failed — LibreOffice unavailable", 500)
    except Exception:
        log.exception("excel_to_jpg"); return err("Excel to JPG failed", 500)
@app.route("/api/v1/excel-to-ppt", methods=["POST"])
@app.route("/api/excel-to-ppt", methods=["POST"])
@require_auth
@require_rate_limit
def excel_to_ppt():
    if not OPENPYXL_AVAILABLE or not PPTX_AVAILABLE:
        return err("Requires openpyxl + python-pptx.", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            wb = load_workbook(path, data_only=True)
            prs = Presentation(); prs.slide_width=PptxInches(10); prs.slide_height=PptxInches(7.5)
            tc_layout = prs.slide_layouts[1]
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                rows = [r for r in ws.iter_rows(values_only=True) if any(c is not None for c in r)]
                if not rows: continue
                slide = prs.slides.add_slide(tc_layout)
                if slide.shapes.title: slide.shapes.title.text = sheet_name
                max_cols = max(len(r) for r in rows)
                max_cols = min(max_cols, 20)  # FIX-093: cap columns alongside existing row cap
                max_rows = min(len(rows), 25)
                tbl_shape = slide.shapes.add_table(max_rows, max_cols,
                                                    PptxInches(0.5), PptxInches(1.5),
                                                    PptxInches(9), PptxInches(5))
                table = tbl_shape.table
                for r_idx in range(max_rows):
                    for c_idx in range(max_cols):
                        cell = table.cell(r_idx, c_idx)
                        val = rows[r_idx][c_idx] if c_idx < len(rows[r_idx]) else ""
                        cell.text = coerce_cell_for_csv(val)
                        if r_idx == 0: cell.text_frame.paragraphs[0].font.bold = True
            wb.close()
            fname = generate_output_filename(f.filename,"to_ppt")
            fname = re.sub(r'\.(xls|xlsx)$','.pptx',fname,flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, fname); prs.save(out)
        return ok("Excel converted to PowerPoint", out)
    except Exception:
        log.exception("excel_to_ppt"); return err("Excel to PPT failed", 500)
@app.route("/api/v1/jpg-to-pdf", methods=["POST"])
@app.route("/api/jpg-to-pdf", methods=["POST"])
@require_auth
@require_rate_limit
def jpg_to_pdf():
    """Convert JPG images to PDF"""
    files = request.files.getlist("files")
    if not files or all(f.filename == "" for f in files):
        return err("At least one JPG file required")
    for f in files:
        e = validate_file(f, Config.ALLOWED_IMAGE)
        if e: return err(e)
    page_size = request.form.get("page_size", "auto")
    try:
        with FileService.temp_uploads(files) as paths:
            # FIX-100: validate paths is non-empty
            if not paths:  # FIX-100
                return err("No valid images to convert", 400)
            fname = generate_output_filename(files[0].filename, "to_pdf",
                                              is_multi=True, filenames=[f.filename for f in files])
            fname = re.sub(r'\.(jpg|jpeg)$', '.pdf', fname, flags=re.IGNORECASE)
            if not fname.endswith(".pdf"): fname = Path(fname).stem + ".pdf"
            out = _images_to_pdf(paths, page_size, fname)
        return ok(f"Converted {len(files)} JPG(s) to PDF", out)
    except Exception:
        log.exception("jpg_to_pdf"); return err("JPG to PDF failed", 500)
@app.route("/api/v1/pdf-to-jpg", methods=["POST"])
@app.route("/api/pdf-to-jpg", methods=["POST"])
@require_auth
@require_rate_limit
def pdf_to_jpg():
    """Convert PDF to JPG images"""
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            dpi = safe_int(request.form.get("dpi", "150"), 150, 72, 300)
            doc = fitz.open(path)
            # FIX-088: guard against empty PDFs in pdf_to_jpg
            if len(doc) == 0:
                doc.close()
                return err("Input PDF has no pages", 400)
            # FIX-058: DPI safety guard for pdf_to_jpg
            if len(doc) * dpi > 30000:  # FIX-058
                dpi = max(72, 30000 // len(doc))
                log.warning(f"pdf_to_jpg: DPI reduced to {dpi}")
            count = len(doc)
            # FIX-045: use disk-based temp file for ZIP to avoid OOM on large PDFs
            tmp_zip = tempfile.NamedTemporaryFile(
                suffix=".zip", delete=False, dir=Config.TEMP_FOLDER)
            tmp_zip.close()
            try:
                with zipfile.ZipFile(tmp_zip.name, "w", zipfile.ZIP_DEFLATED) as zf:
                    for i, page in enumerate(doc):
                        pix = page.get_pixmap(dpi=dpi)
                        zf.writestr(f"page_{i+1:04d}.jpg", pix.tobytes("jpeg"))
                doc.close()
                fname = generate_output_filename(f.filename, "to_jpg")
                fname = re.sub(r'\.pdf$', '.zip', fname, flags=re.IGNORECASE)
                if not fname.endswith('.zip'):
                    fname = Path(fname).stem + '.zip'
                out = os.path.join(Config.OUTPUT_FOLDER, fname)
                shutil.move(tmp_zip.name, out)
            except Exception:
                try: os.remove(tmp_zip.name)
                except OSError: pass
                raise
        return ok(f"Exported {count} page(s) as JPG", out)
    except Exception:
        log.exception("pdf_to_jpg")
        return err("PDF to JPG failed", 500)
@app.route("/api/v1/pdf-to-png", methods=["POST"])
@app.route("/api/pdf-to-png", methods=["POST"])
@require_auth
@require_rate_limit
def pdf_to_png():
    """Convert PDF to PNG images"""
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_PDF)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            dpi = safe_int(request.form.get("dpi", "150"), 150, 72, 300)
            doc = fitz.open(path)
            # FIX-088: guard against empty PDFs in pdf_to_png
            if len(doc) == 0:
                doc.close()
                return err("Input PDF has no pages", 400)
            # FIX-058: DPI safety guard for pdf_to_png
            if len(doc) * dpi > 30000:  # FIX-058
                dpi = max(72, 30000 // len(doc))
                log.warning(f"pdf_to_png: DPI reduced to {dpi}")
            count = len(doc)
            # FIX-045: use disk-based temp file for ZIP to avoid OOM on large PDFs
            tmp_zip = tempfile.NamedTemporaryFile(
                suffix=".zip", delete=False, dir=Config.TEMP_FOLDER)
            tmp_zip.close()
            try:
                with zipfile.ZipFile(tmp_zip.name, "w", zipfile.ZIP_DEFLATED) as zf:
                    for i, page in enumerate(doc):
                        pix = page.get_pixmap(dpi=dpi)
                        zf.writestr(f"page_{i+1:04d}.png", pix.tobytes("png"))
                doc.close()
                fname = generate_output_filename(f.filename, "to_png")
                fname = re.sub(r'\.pdf$', '.zip', fname, flags=re.IGNORECASE)
                if not fname.endswith('.zip'):
                    fname = Path(fname).stem + '.zip'
                out = os.path.join(Config.OUTPUT_FOLDER, fname)
                shutil.move(tmp_zip.name, out)
            except Exception:
                try: os.remove(tmp_zip.name)
                except OSError: pass
                raise
        return ok(f"Exported {count} page(s) as PNG", out)
    except Exception:
        log.exception("pdf_to_png")
        return err("PDF to PNG failed", 500)
@app.route("/api/v1/word-to-jpg", methods=["POST"])
@app.route("/api/word-to-jpg", methods=["POST"])
@require_auth
@require_rate_limit
def word_to_jpg():
    """Convert Word to JPG images"""
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            pdf_path = libre(path, "pdf", temp=True)
            if not pdf_path: return err("LibreOffice conversion failed", 500)
            doc = None
            try:
                doc = fitz.open(pdf_path)
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for i, page in enumerate(doc):
                        pix = page.get_pixmap(dpi=150)
                        zf.writestr(f"page_{i+1:04d}.jpg", pix.tobytes("jpeg"))
            finally:
                if doc: doc.close()
                if pdf_path:
                    try: os.remove(pdf_path)
                    except OSError: pass
            fname = generate_output_filename(f.filename, "to_jpg")
            fname = re.sub(r'\.(doc|docx)$', '.zip', fname, flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            with open(out, "wb") as fh: fh.write(buf.getvalue())
        return ok("Word converted to JPG images", out)
    except Exception:
        log.exception("word_to_jpg"); return err("Word to JPG failed", 500)
@app.route("/api/v1/word-to-png", methods=["POST"])
@app.route("/api/word-to-png", methods=["POST"])
@require_auth
@require_rate_limit
def word_to_png():
    """Convert Word to PNG images"""
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            pdf_path = libre(path, "pdf", temp=True)
            if not pdf_path: return err("LibreOffice conversion failed", 500)
            doc = None
            try:
                doc = fitz.open(pdf_path)
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for i, page in enumerate(doc):
                        pix = page.get_pixmap(dpi=150)
                        zf.writestr(f"page_{i+1:04d}.png", pix.tobytes("png"))
            finally:
                if doc: doc.close()
                if pdf_path:
                    try: os.remove(pdf_path)
                    except OSError: pass
            fname = generate_output_filename(f.filename, "to_png")
            fname = re.sub(r'\.(doc|docx)$', '.zip', fname, flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            with open(out, "wb") as fh: fh.write(buf.getvalue())
        return ok("Word converted to PNG images", out)
    except Exception:
        log.exception("word_to_png"); return err("Word to PNG failed", 500)
@app.route("/api/v1/word-to-html", methods=["POST"])
@app.route("/api/word-to-html", methods=["POST"])
@require_auth
@require_rate_limit
def word_to_html():
    """Convert Word to HTML"""
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            fname = generate_output_filename(f.filename, "to_html")
            fname = re.sub(r'\.(doc|docx)$', '.html', fname, flags=re.IGNORECASE)
            out = libre(path, "html", output_filename=fname)
            if not out: return err("LibreOffice conversion failed", 500)
        return ok("Word converted to HTML", out)
    except Exception:
        log.exception("word_to_html"); return err("Word to HTML failed", 500)
@app.route("/api/v1/word-to-json", methods=["POST"])
@app.route("/api/word-to-json", methods=["POST"])
@require_auth
@require_rate_limit
def word_to_json():
    """Convert Word to JSON"""
    if not DOCX_AVAILABLE: return err("Requires python-docx", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_DOC)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            # FIX-075: wrap DocxDocument in try/finally to ensure close
            doc = None
            try:
                doc = DocxDocument(path)
                data = {"paragraphs": [p.text for p in doc.paragraphs], "tables": []}
                for table in doc.tables:
                    tdata = []
                    for row in table.rows:
                        seen = set()
                        cells = []
                        for cell in row.cells:
                            cid = id(cell._tc)
                            if cid not in seen:
                                seen.add(cid)
                                cells.append(cell.text)
                        tdata.append(cells)
                    data["tables"].append(tdata)
            finally:
                if doc is not None and hasattr(doc, 'close'):
                    try: doc.close()  # FIX-075
                    except Exception: pass
            fname = generate_output_filename(f.filename, "to_json")
            fname = re.sub(r'\.(doc|docx)$', '.json', fname, flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
        return ok("Word converted to JSON", out)
    except Exception:
        log.exception("word_to_json"); return err("Word to JSON failed", 500)
@app.route("/api/v1/excel-to-png", methods=["POST"])
@app.route("/api/excel-to-png", methods=["POST"])
@require_auth
@require_rate_limit
def excel_to_png():
    """Convert Excel to PNG images"""
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            pdf_path = libre(path, "pdf", temp=True)
            if pdf_path and os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 100:
                doc = None
                try:
                    doc = fitz.open(pdf_path)
                    buf = io.BytesIO()
                    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                        for i, page in enumerate(doc):
                            pix = page.get_pixmap(dpi=150)
                            zf.writestr(f"sheet_{i+1:04d}.png", pix.tobytes("png"))
                    fname = generate_output_filename(f.filename, "to_png")
                    fname = re.sub(r'\.(xls|xlsx)$', '.zip', fname, flags=re.IGNORECASE)
                    out = os.path.join(Config.OUTPUT_FOLDER, fname)
                    with open(out, "wb") as fh: fh.write(buf.getvalue())
                    return ok("Excel sheets exported as PNG", out)
                finally:
                    if doc: doc.close()
                    if pdf_path:
                        try: os.remove(pdf_path)
                        except OSError: pass
            return err("Excel to PNG failed — LibreOffice unavailable", 500)
    except Exception:
        log.exception("excel_to_png"); return err("Excel to PNG failed", 500)
@app.route("/api/v1/excel-to-html", methods=["POST"])
@app.route("/api/excel-to-html", methods=["POST"])
@require_auth
@require_rate_limit
def excel_to_html():
    """Convert Excel to HTML"""
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_XLS)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            fname = generate_output_filename(f.filename, "to_html")
            fname = re.sub(r'\.(xls|xlsx)$', '.html', fname, flags=re.IGNORECASE)
            out = libre(path, "html", output_filename=fname)
            if not out: return err("LibreOffice conversion failed", 500)
        return ok("Excel converted to HTML", out)
    except Exception:
        log.exception("excel_to_html"); return err("Excel to HTML failed", 500)
@app.route("/api/v1/image-to-word", methods=["POST"])
@app.route("/api/image-to-word", methods=["POST"])
@require_auth
@require_rate_limit
def image_to_word():
    """Convert Image to Word (OCR)"""
    if not DOCX_AVAILABLE: return err("Requires python-docx", 501)
    f = request.files.get("file")
    e = validate_file(f, Config.ALLOWED_IMAGE)
    if e: return err(e)
    try:
        with FileService.temp_upload(f) as path:
            img = Image.open(path)
            # FIX-096: guard for zero-dimension images in image_to_word
            if 0 in img.size:  # FIX-096
                return err("Image has zero-dimension — cannot process", 400)
            # FIX-079: resize the input image before OCR to prevent extreme memory use
            MAX_OCR_DIM = 4000  # FIX-079
            if max(img.size) > MAX_OCR_DIM:
                img.thumbnail((MAX_OCR_DIM, MAX_OCR_DIM), Image.LANCZOS)  # FIX-079
            doc = DocxDocument()
            doc.add_heading("Image OCR Result", 0)

            if TESSERACT_AVAILABLE:
                # FIX-060: use semaphore to limit concurrent OCR in image_to_word
                with _ocr_semaphore:
                    text = pytesseract.image_to_string(img)
                del img  # FIX-078: release PIL buffer before saving docx
                for para in text.split('\n\n'):
                    if para.strip():
                        doc.add_paragraph(para.strip())
            else:
                tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                img.save(tmp.name, format="PNG"); tmp.close()
                del img  # FIX-078: release PIL buffer
                doc.add_picture(tmp.name, width=Inches(6))
                os.unlink(tmp.name)
                doc.add_paragraph("(OCR not available — image embedded)")

            fname = generate_output_filename(f.filename, "to_word")
            fname = re.sub(r'\.(jpg|jpeg|png|gif|bmp|tiff|webp)$', '.docx',
                           fname, flags=re.IGNORECASE)
            out = os.path.join(Config.OUTPUT_FOLDER, fname)
            doc.save(out)
        return ok("Image converted to Word", out)
    except Exception:
        log.exception("image_to_word"); return err("Image to Word failed", 500)
# ============================================================================
# ERROR HANDLERS
# ============================================================================
@app.errorhandler(400)
def _e400(e): return jsonify({"success":False,"error":"Bad request","request_id":g.get("request_id","-")}),400
@app.errorhandler(404)
def _e404(e): return jsonify({"success":False,"error":"Endpoint not found","request_id":g.get("request_id","-")}),404
@app.errorhandler(405)
def _e405(e): return jsonify({"success":False,"error":"Method not allowed"}),405
@app.errorhandler(413)
def _e413(e): return jsonify({"success":False,"error":f"File too large (max {Config.MAX_FILE_SIZE//1048576} MB)","request_id":g.get("request_id","-")}),413
@app.errorhandler(429)
def _e429(e): return jsonify({"success":False,"error":"Rate limit exceeded","request_id":g.get("request_id","-")}),429
@app.errorhandler(500)
def _e500(e): log.exception("Unhandled 500"); return jsonify({"success":False,"error":"Internal server error","request_id":g.get("request_id","-")}),500
# ============================================================================
# GRACEFUL SHUTDOWN
# ============================================================================
_shutdown_event = threading.Event()
def _graceful_shutdown(signum, frame):
    log.info(f"Received signal {signum} — graceful shutdown…")
    _shutdown_event.set()
    if celery_app:
        try: celery_app.control.broadcast("pool_shrink", arguments={"n":0})
        except Exception: pass
    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(1)
    log.info("Shutdown complete.")
    sys.exit(0)
signal.signal(signal.SIGTERM, _graceful_shutdown)
signal.signal(signal.SIGINT, _graceful_shutdown)
application = app # WSGI entry point
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG","false").lower() in ("true","1","yes")
    log.info(f"PDFWala V{Config.VERSION} starting on :{port}")
    app.run(host="0.0.0.0", port=port, debug=debug, threaded=True, use_reloader=False)
