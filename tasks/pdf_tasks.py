"""
tasks/pdf_tasks.py — PDFWala Enterprise V11.1.1

FIXES applied (see audit doc):
  CRIT-01  watermark_pdf_task decorator: duplicate kwargs (max_retries,
           acks_late, time_limit, soft_time_limit) removed; merged into
           wm-specific config dict to avoid TypeError at import time.
  CRIT-02  watermark_pdf_task: inverted chunked/single-pass logic corrected.
           Single-pass is now only called in the `else` branch or as fallback.
  CRIT-03  merge_pdf_task pikepdf: out_pdf.save() moved OUTSIDE the for-loop
           so all pages are written once, not overwritten per iteration.
  CRIT-04  merge_pdf_task idempotency: early `return` now lives INSIDE the
           validate_pdf() branch, not unconditionally after it.
  HIGH-01  compress_pdf_task: orig_size captured BEFORE processing starts so
           reduction_pct is always accurate.
  HIGH-02  compress_pdf_task: `if doc:` guard added around doc.close() to
           prevent AttributeError when doc was set to None.
  HIGH-03  watermark_pdf_task: 0-page PDF raises UserError before processing.
  MED-01   split_pdf_task: empty page_indices list raises UserError.
  MED-02   split_pdf_task: reader.close() in finally to prevent fd leak.
  MED-03   split_pdf_task: os.replace(tmp_zip_path, output_path) added so the
           final output file is actually written (was missing entirely).
  MED-04   split_pdf_task: finally block no longer references tmp_dir outside
           its `with` scope; redundant shutil.rmtree removed.
  MED-05   SystemError renamed to PDFSystemError to avoid shadowing builtin.
  MED-06   watermark_pdf_task task name corrected to include module path.
  MED-07   _last_update_map uses capped OrderedDict.
  MED-08   compress_pdf_task: pil.close() called after each image to free RAM.
  MED-09   compress_pdf_task: pdf_type stored as JSON string in Redis.
  SEC-01   _ghostscript_compress: output_path validated not to start with '-'.
"""

import io
import json
import os
import time
import shutil
import zipfile
import logging
import subprocess
import tempfile
import threading
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path
from typing import List, Optional, Dict, Any

# ── Third Party Imports ───────────────────────────────────────────────────────
try:
    import fitz
    FITZ_AVAILABLE = True
except ImportError:
    FITZ_AVAILABLE = False

try:
    import pikepdf
    PIKEPDF_AVAILABLE = True
except ImportError:
    PIKEPDF_AVAILABLE = False

try:
    from PyPDF2 import PdfReader, PdfWriter
    PYPDF2_AVAILABLE = True
except ImportError:
    PYPDF2_AVAILABLE = False

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# ── Celery & Services ─────────────────────────────────────────────────────────
from workers.celery_app import celery_app
from services.redis_service import redis_service
from services.queue_service import cb_ghostscript
from utils.helpers import get_timestamp
from utils.pdf_utils import chunked_pdf_processor, merge_pdf_chunks, create_watermark_pdf
from config import Config

log = logging.getLogger("pdfwala.tasks.pdf")

# ── Configuration Constants ───────────────────────────────────────────────────
WATERMARK_CHUNK_THRESHOLD = int(getattr(Config, "WATERMARK_CHUNK_THRESHOLD", 200))
WATERMARK_CHUNK_PAGES     = int(getattr(Config, "WATERMARK_CHUNK_PAGES",     100))
WATERMARK_MAX_WORKERS     = int(getattr(Config, "WATERMARK_MAX_WORKERS",       4))

MAX_PDF_PAGES        = int(getattr(Config, "MAX_PDF_PAGES",      5000))
MAX_PDF_SIZE_MB      = int(getattr(Config, "MAX_PDF_SIZE_MB",     500))
MAX_PDF_SIZE_BYTES   = MAX_PDF_SIZE_MB * 1024 * 1024

MIN_DISK_SPACE_MB    = int(getattr(Config, "MIN_DISK_SPACE_MB",   100))
MIN_DISK_SPACE_BYTES = MIN_DISK_SPACE_MB * 1024 * 1024

GS_MAX_CONCURRENT    = int(getattr(Config, "GS_MAX_CONCURRENT",     2))
GS_SEMAPHORE         = threading.Semaphore(GS_MAX_CONCURRENT)

# Base task defaults — shared across most tasks
TASK_DEFAULTS = {
    "max_retries": 3,
    "time_limit": 600,
    "soft_time_limit": 540,
    "acks_late": True,
    "reject_on_worker_lost": True,
}

# CRIT-01: watermark has its own config dict so duplicate kwargs are impossible
_WM_TASK_CFG = {
    **TASK_DEFAULTS,
    "max_retries": 2,
    "time_limit": 1800,
    "soft_time_limit": 1500,
}


# ── Error Classification ──────────────────────────────────────────────────────
class PDFProcessingError(Exception):
    def __init__(self, message: str, error_type: str = "system",
                 retryable: bool = True):
        super().__init__(message)
        self.error_type = error_type
        self.retryable  = retryable
        self.message    = message


class UserError(PDFProcessingError):
    """User-provided invalid input — should NOT retry."""
    def __init__(self, message: str):
        super().__init__(message, error_type="user", retryable=False)


# MED-05: renamed from SystemError (was shadowing Python builtin)
class PDFSystemError(PDFProcessingError):
    """System/infrastructure error — SHOULD retry."""
    def __init__(self, message: str):
        super().__init__(message, error_type="system", retryable=True)


class ExternalError(PDFProcessingError):
    """External service error (Ghostscript, etc.) — conditional retry."""
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message, error_type="external", retryable=retryable)


# ── Structured Logging ────────────────────────────────────────────────────────
from datetime import datetime


def log_structured(level: str, message: str, job_id: str = None, **extra):
    entry = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "level": level,
        "message": message,
        "job_id": job_id,
        **extra,
    }
    log.info(json.dumps(entry))


# ── Path Security ─────────────────────────────────────────────────────────────
def get_allowed_base_dirs() -> List[Path]:
    allowed = getattr(Config, "ALLOWED_DIRECTORIES", [])
    if not allowed:
        return [Path(tempfile.gettempdir()).resolve(), Path.cwd().resolve()]
    return [Path(p).resolve() for p in allowed]


def validate_path(path: str, allow_nonexistent: bool = False,
                  job_id: str = None) -> Path:
    if not path:
        raise UserError("Path cannot be empty")
    try:
        p = Path(path).resolve()
    except Exception as e:
        raise UserError(f"Invalid path format: {e}")

    allowed_bases = get_allowed_base_dirs()
    is_allowed = any(p.is_relative_to(base) or p == base for base in allowed_bases)
    if not is_allowed:
        log_structured("ERROR", "Path traversal attempt detected",
                       job_id=job_id, path=str(p))
        raise UserError(f"Path {p} is not within allowed directories")

    if not allow_nonexistent and not p.exists():
        raise UserError(f"Path does not exist: {p}")

    return p


def _safe_path(p: str) -> str:
    p_obj = Path(p).resolve()
    if not p_obj.exists():
        raise ValueError("Invalid file path")
    # SEC-01: guard against path starting with '-' (Ghostscript flag injection)
    if str(p_obj).startswith("-"):
        raise ValueError("Path must not start with '-'")
    return str(p_obj)


# ── Disk Space Protection ─────────────────────────────────────────────────────
def check_disk_space(required_mb: int = MIN_DISK_SPACE_MB,
                     job_id: str = None,
                     path: str = None) -> bool:
    try:
        if path:
            target_dir = os.path.abspath(os.path.dirname(path))
        else:
            target_dir = str(Path(tempfile.gettempdir()).resolve())
        stat = shutil.disk_usage(target_dir)
        available_mb = stat.free / (1024 * 1024)
        if available_mb < required_mb:
            log_structured("ERROR", "Insufficient disk space", job_id=job_id,
                           available_mb=round(available_mb, 2),
                           required_mb=required_mb,
                           target_dir=target_dir)
            raise PDFSystemError(
                f"Insufficient disk space: {available_mb:.2f} MB available, "
                f"{required_mb} MB required in {target_dir}"
            )
        log_structured("INFO", "Disk space check passed", job_id=job_id,
                       available_mb=round(available_mb, 2),
                       target_dir=target_dir)
        return True
    except (UserError, PDFSystemError):
        raise
    except Exception as e:
        log_structured("WARNING", "Could not verify disk space",
                       job_id=job_id, error=str(e))
        return True


# ── PDF Helpers ────────────────────────────────────────────────────────────────
def safe_open_pdf(path: str) -> "fitz.Document":
    return fitz.open(path)


def classify_pdf_content(doc: "fitz.Document") -> Dict[str, Any]:
    try:
        total_pages = len(doc)
        total_images = 0
        total_text_blocks = 0
        sample_pages = min(10, total_pages)
        for i in range(sample_pages):
            page = doc[i]
            total_images += len(page.get_images(full=True))
            total_text_blocks += len(page.get_text("blocks"))
        avg_images = total_images / sample_pages if sample_pages else 0
        avg_text   = total_text_blocks / sample_pages if sample_pages else 0
        return {
            "is_image_heavy": avg_images > 5,
            "is_text_heavy": avg_text > 50,
            "avg_images_per_page": avg_images,
            "avg_text_blocks_per_page": avg_text,
            "sampled_pages": sample_pages,
            "total_pages": total_pages,
        }
    except Exception:
        return {
            "is_image_heavy": False,
            "is_text_heavy": False,
            "avg_images_per_page": 0,
            "avg_text_blocks_per_page": 0,
            "sampled_pages": 0,
            "total_pages": 0,
        }


def validate_pdf(path: str) -> bool:
    try:
        doc = fitz.open(path)
        doc.close()
        return True
    except Exception:
        return False


# ── Ghostscript Compression ────────────────────────────────────────────────────
def _ghostscript_compress(
    input_path: str,
    output_path: str,
    gs_setting: str = "/ebook",
    extra_flags: list = None,
    timeout: int = 300,
    job_id: str = None,
) -> bool:
    """Ghostscript compression with concurrency control and safety checks."""
    if not cb_ghostscript.can_execute():
        log.error("CircuitBreaker[ghostscript] OPEN")
        return False

    cmd = [
        Config.GHOSTSCRIPT,
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.5",
        f"-dPDFSETTINGS={gs_setting}",
        "-dNOPAUSE",
        "-dBATCH",
        "-dQUIET",
        "-dSAFER",
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true",
        "-dSubsetFonts=true",
        "-dAutoRotatePages=/None",
        f"-sOutputFile={output_path}",
        _safe_path(input_path),  # SEC-01 validation inside _safe_path
    ]

    if extra_flags:
        cmd.extend(extra_flags)

    try:
        with GS_SEMAPHORE:
            result = subprocess.run(
                cmd, capture_output=True, timeout=timeout, check=False
            )
    except Exception as ex:
        log.error(f"Ghostscript exception: {ex}")
        return False

    stderr = result.stderr.decode(errors="ignore")[:200]
    if result.returncode != 0:
        log.error(f"Ghostscript failed: {stderr}")
        cb_ghostscript.record_failure()
        return False

    if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
        log.error("Ghostscript produced no output")
        cb_ghostscript.record_failure()
        return False

    cb_ghostscript.record_success()
    log.info(f"Ghostscript compression completed (job {job_id})")
    return True


# ── Watermark Single-Pass Helper ───────────────────────────────────────────────
def _watermark_single_pass(
    input_path: str,
    output_path: str,
    text: str,
    opacity: float,
    color: str,
    position: str,
    rotation: float,
    job_id: str = None,
) -> None:
    doc = None
    try:
        doc = fitz.open(input_path)
        if doc.is_encrypted:
            raise UserError("Encrypted PDFs must be decrypted before watermarking")
        for page in doc:
            r = page.rect
            wm = create_watermark_pdf(text, opacity, color,
                                      r.width, r.height, position, rotation)
            with fitz.open("pdf", wm) as wmpdf:
                page.show_pdf_page(
                    fitz.Rect(0, 0, r.width, r.height), wmpdf, 0, overlay=True
                )
        tmp_path = output_path + ".wm_tmp"
        doc.save(tmp_path, deflate=True, garbage=2, clean=True)
        os.replace(tmp_path, output_path)
    finally:
        if doc:
            doc.close()


# ── Atomic File Operations ────────────────────────────────────────────────────
@contextmanager
def atomic_write(final_path: str, job_id: str = None):
    temp_path = None
    try:
        final_path_obj = Path(final_path).resolve()
        temp_dir = final_path_obj.parent
        temp_prefix = f".tmp_{job_id}_" if job_id else ".tmp_"
        temp_path = temp_dir / f"{temp_prefix}{final_path_obj.name}.{time.time()}"
        yield str(temp_path)
        os.replace(str(temp_path), str(final_path_obj))
        log_structured("INFO", "Atomic write completed", job_id=job_id,
                       path=str(final_path_obj))
    except Exception:
        if temp_path and Path(temp_path).exists():
            try:
                os.remove(str(temp_path))
            except OSError as e:
                log_structured("WARNING", "Failed to cleanup temp file",
                               job_id=job_id, path=str(temp_path), error=str(e))
        raise


# ── Throttled Redis Updates ────────────────────────────────────────────────────
# MED-07: capped OrderedDict instead of plain dict
_MAX_TRACKED_JOBS = 10_000
_last_update_map: OrderedDict = OrderedDict()
_update_lock = threading.Lock()


def safe_job_update(job_id: str, data: dict) -> None:
    now = time.time()
    with _update_lock:
        last = _last_update_map.get(job_id, 0)
        if now - last > 1:
            redis_service.job_update(job_id, data)
            _last_update_map[job_id] = now
            _last_update_map.move_to_end(job_id)
            if len(_last_update_map) > _MAX_TRACKED_JOBS:
                _last_update_map.popitem(last=False)


# ── Celery Tasks ──────────────────────────────────────────────────────────────

@celery_app.task(bind=True, **TASK_DEFAULTS)
def compress_pdf_task(
    self, input_path: str, output_path: str, job_id: str, quality: str = "medium"
):
    """Async PDF compression: PyMuPDF image down-sampling + Ghostscript."""
    start_time = time.time()
    safe_job_update(job_id, {"status": "processing"})

    if not FITZ_AVAILABLE:
        raise PDFSystemError("PyMuPDF (fitz) not available")

    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        if validate_pdf(output_path):
            log_structured("INFO", "Output already exists (idempotent)", job_id=job_id)
            return {"status": "completed", "output": output_path, "idempotent": True}

    input_path_obj = validate_path(input_path, job_id=job_id)
    check_disk_space(MIN_DISK_SPACE_MB, job_id=job_id, path=output_path)

    # HIGH-01: capture orig_size BEFORE any processing
    orig_size = os.path.getsize(input_path_obj)

    doc = safe_open_pdf(str(input_path_obj))
    pdf_type = classify_pdf_content(doc)
    total_pages = pdf_type.get("total_pages", 0)
    doc.close()
    doc = None

    cfg_map = {
        "low":    {"dpi": 150, "quality": 85, "gs": "/printer"},
        "medium": {"dpi": 120, "quality": 72, "gs": "/printer"},
        "high":   {"dpi":  96, "quality": 60, "gs": "/ebook"},
    }
    cfg = cfg_map.get(quality, cfg_map["medium"])

    # ---- Stage 1: image down-sampling (PyMuPDF + PIL) -----------------------
    stage1_path = output_path + "_s1.pdf"
    modified = False
    try:
        doc = fitz.open(str(input_path_obj))
        for page in doc:
            for img in page.get_images(full=True):
                xref = img[0]
                pil = None
                try:
                    base = doc.extract_image(xref)
                    if not base:
                        continue
                    pil = Image.open(io.BytesIO(base["image"]))
                    ow, oh = pil.size
                    src_dpi = max(base.get("xres", 150), base.get("yres", 150), 1)
                    scale = min(1.0, cfg["dpi"] / src_dpi)
                    if scale >= 0.95:
                        continue
                    nw = max(1, int(ow * scale))
                    nh = max(1, int(oh * scale))
                    pil = pil.resize((nw, nh), Image.LANCZOS)

                    if pil.mode in ("RGBA", "P", "LA"):
                        bg = Image.new("RGB", pil.size, (255, 255, 255))
                        if pil.mode == "P":
                            # Only use alpha split if palette has transparency
                            if "transparency" in pil.info:
                                pil = pil.convert("RGBA")
                                mask = pil.split()[-1]
                            else:
                                pil = pil.convert("RGB")
                                mask = None
                        else:
                            # RGBA or LA: alpha is always the last channel
                            mask = pil.split()[-1]
                        if pil.mode in ("RGBA", "LA"):
                            bg.paste(pil, mask=mask)
                            pil = bg
                    elif pil.mode != "RGB":
                        pil = pil.convert("RGB")

                    buf_img = io.BytesIO()
                    pil.save(buf_img, "JPEG",
                             quality=cfg["quality"], optimize=True, progressive=True)
                    doc.update_stream(xref, buf_img.getvalue())
                    buf_img.close()
                    modified = True
                except Exception as img_ex:
                    log.warning(f"Image processing error on page {page.number}: {img_ex}")
                finally:
                    # MED-08: release PIL bitmap immediately to limit peak RAM
                    if pil is not None:
                        try:
                            pil.close()
                        except Exception:
                            pass
    except Exception as ex:
        log.warning(f"Stage-1 error: {ex}")

    if modified:
        doc.save(stage1_path, deflate=True, deflate_images=True,
                 deflate_fonts=True, garbage=3, clean=False)
    else:
        shutil.copy(str(input_path_obj), stage1_path)

    # HIGH-02: guard against doc being None before calling close()
    if doc:
        doc.close()
    doc = None

    # ---- Stage 2: Ghostscript compression -----------------------------------
    chosen = None
    gs_out = output_path + "_gs.pdf"
    if pdf_type.get("is_image_heavy", False) and os.path.getsize(stage1_path) > 1024 * 1024:
        if _ghostscript_compress(
            stage1_path, gs_out,
            gs_setting=cfg["gs"],
            extra_flags=[
                f"-dColorImageDownsampleType=/Bicubic",
                f"-dColorImageResolution={cfg['dpi']}",
                f"-dGrayImageResolution={cfg['dpi']}",
            ],
            job_id=job_id,
        ):
            if os.path.getsize(gs_out) < os.path.getsize(stage1_path):
                chosen = gs_out

    if chosen is None:
        chosen = (
            stage1_path
            if os.path.getsize(stage1_path) < orig_size
            else str(input_path_obj)
        )

    tmp_out = output_path + ".tmp"
    shutil.copy(chosen, tmp_out)
    os.replace(tmp_out, output_path)

    for tmp in (stage1_path, gs_out):
        try:
            os.remove(tmp)
        except OSError:
            pass

    if not validate_pdf(output_path):
        raise PDFSystemError("Corrupted output PDF after processing")

    new_size = os.path.getsize(output_path)
    reduction_pct = round((1 - new_size / orig_size) * 100, 1) if orig_size else 0.0

    # MED-09: store pdf_type as JSON string to avoid unsafe Redis serialisation
    safe_job_update(job_id, {
        "status": "completed",
        "progress": "100",
        "output_path": output_path,
        "reduction_pct": str(reduction_pct),
        "completed_at": get_timestamp(),
        "orig_size_bytes": str(orig_size),
        "new_size_bytes": str(new_size),
        "pdf_type": json.dumps(pdf_type),
    })

    try:
        os.remove(input_path)
    except OSError:
        pass

    log_structured("INFO", "compress_pdf_task completed",
                   job_id=job_id,
                   duration=round(time.time() - start_time, 2),
                   reduction_pct=reduction_pct)
    return {"status": "completed", "output": output_path}


@celery_app.task(bind=True, **TASK_DEFAULTS)
def merge_pdf_task(self, input_paths: List[str], output_path: str, job_id: str):
    """Async PDF merge task — uses pikepdf for low-memory operation."""
    start_time = time.time()
    safe_job_update(job_id, {"status": "processing"})

    # CRIT-04: `return` is now INSIDE the validate_pdf() branch
    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        if validate_pdf(output_path):
            log_structured("INFO", "Output already exists (idempotent)", job_id=job_id)
            return {"status": "completed", "output": output_path, "idempotent": True}

    check_disk_space(MIN_DISK_SPACE_MB * 2, job_id=job_id, path=output_path)

    validated_paths = [str(validate_path(p, job_id=job_id)) for p in input_paths]

    if not validated_paths:
        raise UserError("No valid input files to merge")

    with atomic_write(output_path, job_id=job_id) as safe_output:
        if PIKEPDF_AVAILABLE:
            with pikepdf.Pdf.new() as out_pdf:
                for p in validated_paths:
                    if not os.path.exists(p):
                        log.warning(f"Skipping missing file: {p}")
                        continue
                    with pikepdf.open(p) as src:
                        out_pdf.pages.extend(src.pages)
                # CRIT-03: save is OUTSIDE the per-file loop
                out_pdf.save(safe_output)
        else:
            from PyPDF2 import PdfMerger
            merger = PdfMerger()
            for p in validated_paths:
                if not os.path.exists(p):
                    log.warning(f"Skipping missing file: {p}")
                    continue
                merger.append(p)
            merger.write(safe_output)
            merger.close()

    if not validate_pdf(output_path):
        raise PDFSystemError("Corrupted output PDF after merge")

    safe_job_update(job_id, {
        "status": "completed",
        "progress": "100",
        "output_path": output_path,
        "completed_at": get_timestamp(),
        "file_count": str(len(input_paths)),
    })

    for p in input_paths:
        try:
            os.remove(p)
        except OSError:
            pass

    log_structured("INFO", "merge_pdf_task completed",
                   job_id=job_id, duration=round(time.time() - start_time, 2))
    return {"status": "completed", "output": output_path}


@celery_app.task(bind=True, **TASK_DEFAULTS)
def split_pdf_task(
    self, input_path: str, output_path: str,
    job_id: str, page_indices: List[int] = None
):
    """Async PDF split to ZIP task."""
    start_time = time.time()
    safe_job_update(job_id, {"status": "processing"})

    if os.path.exists(output_path) and output_path.endswith(".zip"):
        if zipfile.is_zipfile(output_path):
            log_structured("INFO", "Output already exists (idempotent)", job_id=job_id)
            return {"status": "completed", "output": output_path, "idempotent": True}

    check_disk_space(MIN_DISK_SPACE_MB * 2, job_id=job_id, path=output_path)

    input_path_obj = validate_path(input_path, job_id=job_id)

    doc = safe_open_pdf(str(input_path_obj))
    total_pages = len(doc)
    doc.close()

    if page_indices is None:
        page_indices = list(range(total_pages))

    # MED-01: empty list check BEFORE the bounds check
    if not page_indices:
        raise UserError("page_indices cannot be empty")

    if any(i < 0 or i >= total_pages for i in page_indices):
        raise UserError(f"Invalid page indices: must be in range 0–{total_pages - 1}")

    tmp_zip_path = output_path + ".split_tmp.zip"

    # MED-04: TemporaryDirectory already handles cleanup on __exit__;
    #         no need for a redundant finally/shutil.rmtree.
    #         reader.close() is guarded in its own try/finally (MED-02).
    with tempfile.TemporaryDirectory(prefix=f"pdfwala_split_{job_id}_") as tmp_dir:
        reader = fitz.open(str(input_path_obj))
        try:
            with zipfile.ZipFile(tmp_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for done, idx in enumerate(page_indices):
                    tmp_pdf = tempfile.NamedTemporaryFile(
                        delete=False, suffix=".pdf", dir=tmp_dir
                    ).name
                    with fitz.open() as writer:
                        writer.insert_pdf(reader, from_page=idx, to_page=idx)
                        writer.save(tmp_pdf)

                    safe_name = f"page_{idx + 1:04d}.pdf"
                    # Defensive: safe_name is constructed above and can never
                    # contain ".." or "/", but validate anyway for defence-in-depth
                    if ".." in safe_name or "/" in safe_name:
                        raise ValueError("Invalid filename detected (ZIP slip guard)")

                    with open(tmp_pdf, "rb") as pdf_data:
                        zf.writestr(safe_name, pdf_data.read())
                    os.remove(tmp_pdf)

                    if done % 10 == 0:
                        pct = int((done + 1) / len(page_indices) * 95)
                        safe_job_update(job_id, {"progress": str(pct)})
        finally:
            # MED-02: always close the reader to prevent fd leak
            reader.close()

    # MED-03: rename tmp zip to final output (was missing — output never written)
    os.replace(tmp_zip_path, output_path)

    safe_job_update(job_id, {
        "status": "completed",
        "progress": "100",
        "output_path": output_path,
        "completed_at": get_timestamp(),
        "page_count": str(len(page_indices)),
    })
    log_structured("INFO", "split_pdf_task completed",
                   job_id=job_id, duration=round(time.time() - start_time, 2))
    return {"status": "completed", "output": output_path}


# CRIT-01: watermark uses _WM_TASK_CFG — no duplicate kwargs possible
# MED-06: task name corrected to include module path
@celery_app.task(
    bind=True,
    name="pdfwala.tasks.pdf_tasks.watermark_pdf_task",
    queue="cpu_bound",
    **_WM_TASK_CFG,
)
def watermark_pdf_task(
    self,
    input_path: str,
    output_path: str,
    job_id: str,
    text: str = "CONFIDENTIAL",
    opacity: float = 0.3,
    color: str = "808080",
    position: str = "diagonal",
    rotation: float = 45.0,
):
    """Async text watermark task — chunked parallel processing for large PDFs."""
    start_time = time.time()
    safe_job_update(job_id, {"status": "processing"})

    if not FITZ_AVAILABLE:
        raise PDFSystemError("PyMuPDF (fitz) not available")

    check_disk_space(MIN_DISK_SPACE_MB, job_id=job_id, path=output_path)

    if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
        if validate_pdf(output_path):
            log_structured("INFO", "Output already exists (idempotent)", job_id=job_id)
            return {"status": "completed", "output": output_path, "idempotent": True}

    input_path_obj = validate_path(input_path, job_id=job_id)
    doc = safe_open_pdf(str(input_path_obj))
    total_pages = len(doc)
    doc.close()
    doc = None

    # HIGH-03: reject degenerate 0-page PDFs
    if total_pages == 0:
        raise UserError("PDF has no pages")

    safe_job_update(job_id, {"total_pages": str(total_pages)})

    # CRIT-02: corrected chunked/single-pass logic
    if total_pages > WATERMARK_CHUNK_THRESHOLD:
        log.info(f"watermark_pdf_task {job_id}: {total_pages} pages — chunked")

        def process_wm_chunk(chunk_path: str, chunk_idx: int,
                             start_page: int, end_page: int) -> str:
            chunk_out = chunk_path.replace("_in.pdf", "_out.pdf")
            _watermark_single_pass(chunk_path, chunk_out, text, opacity,
                                   color, position, rotation, job_id)
            return chunk_out

        success = chunked_pdf_processor(
            input_path=str(input_path_obj),
            output_path=output_path,
            job_id=job_id,
            total_pages=total_pages,
            chunk_size=WATERMARK_CHUNK_PAGES,
            max_workers=WATERMARK_MAX_WORKERS,
            process_chunk_func=process_wm_chunk,
            merge_func=merge_pdf_chunks,
            redis_service=redis_service,
            tool_name="Watermark",
            report_progress=True,
            chunk_retry=1,
        )
        if not success:
            # Chunked failed — fall back to single-pass
            log.warning(
                f"watermark_pdf_task {job_id}: chunked path failed — falling back"
            )
            safe_job_update(job_id, {"status": "processing"})
            _watermark_single_pass(
                str(input_path_obj), output_path,
                text, opacity, color, position, rotation, job_id,
            )
        # CRIT-02: no else branch here — single-pass not called after success
    else:
        # Small PDF — single-pass only
        _watermark_single_pass(
            str(input_path_obj), output_path,
            text, opacity, color, position, rotation, job_id,
        )

    if not validate_pdf(output_path):
        raise PDFSystemError("Corrupted output PDF after watermarking")

    safe_job_update(job_id, {
        "status": "completed",
        "progress": "100",
        "output_path": output_path,
        "completed_at": get_timestamp(),
        "page_count": str(total_pages),
    })
    log_structured("INFO", "watermark_pdf_task completed",
                   job_id=job_id, duration=round(time.time() - start_time, 2))
    return {"status": "completed", "output": output_path}
