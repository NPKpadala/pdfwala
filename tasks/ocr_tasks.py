# --------------------------------------------------------------------------- #
# tasks/ocr_tasks.py — PDFWala Enterprise V11.1.1
#
# FIXES applied (see audit doc):
#   CRIT-01  UserError / log_structured moved to TOP imports (were at bottom)
#   CRIT-02  import threading added (was missing; GS_SEMAPHORE crashed at load)
#   CRIT-03  Dead watermark / ghostscript code removed from this module
#   CRIT-04  _ocr_single_pdf: img=None before try; guarded finally img.close()
#   CRIT-05  pytesseract.image_to_data: timeout= removed (unsupported kwarg)
#   CRIT-06  _ocr_single_pdf: out_doc.save() moved OUT of finally into success
#            path; writes to tmp then os.replace so corrupt partial not saved
#   HIGH-01  ocr_pdf_task: chunked path checks `success` before marking done
#   HIGH-02  Local _throttled_progress_single definition inside task removed;
#            module-level timestamp-based version used instead
#   MED-01   _progress_last_update / _last_update_map use OrderedDict with cap
#   MED-02   pix.close() replaced with `del pix` (not available in all fitz)
#   MED-03   _ocr_single_pdf skip-text threshold raised to >20 chars
#   MED-04   chunk temp files now written to Config.TEMP_FOLDER not /tmp
#   CODE-01  Removed unused `from functools import wraps`
#   CODE-02  Removed unused `from contextlib import contextmanager`
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Standard library imports
# --------------------------------------------------------------------------- #
import io
import json
import os
import re
import shutil
import tempfile
import threading
import time
import logging
import subprocess
from collections import OrderedDict
from pathlib import Path
from typing import List, Optional, Dict, Any
from datetime import datetime

# --------------------------------------------------------------------------- #
# Critical imports that were previously mis-placed at the bottom of the file
# --------------------------------------------------------------------------- #
from utils.errors import UserError          # CRIT-01 fix: must be at top
from utils.logging import log_structured    # CRIT-01 fix: must be at top

# --------------------------------------------------------------------------- #
# Third-party imports (with graceful fallback)
# --------------------------------------------------------------------------- #
try:
    import fitz  # PyMuPDF
    FITZ_AVAILABLE = True
except Exception:  # pragma: no cover
    FITZ_AVAILABLE = False

try:
    import pytesseract
    from pytesseract import Output as TesseractOutput
    TESSERACT_AVAILABLE = True
except Exception:  # pragma: no cover
    TESSERACT_AVAILABLE = False

try:
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = 50_000_000  # protect against memory-bomb images
except Exception:  # pragma: no cover
    Image = None

# --------------------------------------------------------------------------- #
# Celery & Services
# --------------------------------------------------------------------------- #
from workers.celery_app import celery_app
from services.redis_service import redis_service
from utils.helpers import get_timestamp
from utils.pdf_utils import chunked_pdf_processor, merge_pdf_chunks
from config import Config

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
log = logging.getLogger("pdfwala.tasks.ocr")

# --------------------------------------------------------------------------- #
# Language pack validation
# --------------------------------------------------------------------------- #
_AVAILABLE_LANGS: set = set()
if TESSERACT_AVAILABLE:
    try:
        _AVAILABLE_LANGS = set(pytesseract.get_languages(config=""))
        log.info(f"Tesseract langs available: {sorted(_AVAILABLE_LANGS)}")
    except Exception as _lang_ex:  # pragma: no cover
        log.warning(f"Could not enumerate Tesseract language packs: {_lang_ex}")


def _validate_lang(lang: str) -> None:
    """Raise ValueError if any requested language pack is not installed."""
    if not _AVAILABLE_LANGS:
        return
    for pack in lang.split("+"):
        pack = pack.strip()
        if pack and pack not in _AVAILABLE_LANGS:
            raise ValueError(
                f"Language pack '{pack}' is not installed. "
                f"Available packs: {sorted(_AVAILABLE_LANGS)}"
            )


# --------------------------------------------------------------------------- #
# Configuration constants
# --------------------------------------------------------------------------- #
OCR_CHUNK_THRESHOLD  = int(getattr(Config, "OCR_CHUNK_THRESHOLD",  50))
OCR_CHUNK_PAGES      = int(getattr(Config, "OCR_CHUNK_PAGES",      30))
OCR_MAX_WORKERS      = int(getattr(Config, "OCR_MAX_WORKERS",       2))
MIN_DISK_SPACE_MB    = int(getattr(Config, "MIN_DISK_SPACE_MB",    100))
MAX_PDF_PAGES        = int(getattr(Config, "MAX_PDF_PAGES",       2000))
MAX_PDF_SIZE_BYTES   = int(getattr(Config, "MAX_PDF_SIZE_BYTES",
                                   2 * 1024 * 1024 * 1024))


# --------------------------------------------------------------------------- #
# Path security helpers
# --------------------------------------------------------------------------- #
def get_allowed_base_dirs() -> List[Path]:
    allowed = getattr(Config, "ALLOWED_DIRECTORIES", [])
    if not allowed:
        return [Path(tempfile.gettempdir()).resolve(), Path.cwd().resolve()]
    return [Path(p).resolve() for p in allowed]


def validate_path(
    path: str, allow_nonexistent: bool = False, job_id: str = None
) -> Path:
    """Validate that ``path`` is within allowed base directories."""
    if not path:
        raise UserError("Path cannot be empty")
    try:
        p = Path(path).resolve()
    except Exception as e:
        raise UserError(f"Invalid path format: {e}")

    allowed_bases = get_allowed_base_dirs()
    is_allowed = any(p.is_relative_to(base) or p == base for base in allowed_bases)
    if not is_allowed:
        log_structured(
            "ERROR", "Path traversal attempt detected",
            job_id=job_id, path=str(p),
        )
        raise UserError(f"Path {p} is not within allowed directories")

    if not allow_nonexistent and not p.exists():
        raise UserError(f"Path does not exist: {p}")

    return p


def _safe_path(p: str) -> str:
    p_obj = Path(p).resolve()
    if not p_obj.exists():
        raise ValueError("Invalid file path")
    return str(p_obj)


# --------------------------------------------------------------------------- #
# Disk-space protection
# --------------------------------------------------------------------------- #
def check_disk_space(
    required_mb: int = MIN_DISK_SPACE_MB,
    job_id: str = None,
    path: str = None,
) -> bool:
    """Ensure at least ``required_mb`` free space in the output directory."""
    try:
        if path:
            target_dir = os.path.abspath(os.path.dirname(path))
        else:
            target_dir = str(Path(tempfile.gettempdir()).resolve())
        stat = shutil.disk_usage(target_dir)
        available_mb = stat.free / (1024 * 1024)
        if available_mb < required_mb:
            log_structured(
                "ERROR", "Insufficient disk space",
                job_id=job_id,
                available_mb=round(available_mb, 2),
                required_mb=required_mb,
                target_dir=target_dir,
            )
            raise OSError(
                f"Insufficient disk space: {available_mb:.2f} MB available, "
                f"{required_mb} MB required in {target_dir}"
            )
        log_structured(
            "INFO", "Disk space check passed",
            job_id=job_id,
            available_mb=round(available_mb, 2),
            target_dir=target_dir,
        )
        return True
    except (UserError, OSError):
        raise
    except Exception as e:  # pragma: no cover
        log_structured(
            "WARNING", "Could not verify disk space",
            job_id=job_id, error=str(e),
        )
        return True


# --------------------------------------------------------------------------- #
# General file helpers
# --------------------------------------------------------------------------- #
def _safe_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _copy_to_temp(input_path: str, suffix: str = ".pdf") -> str:
    fd, tmp = tempfile.mkstemp(suffix=suffix, dir=Config.TEMP_FOLDER)
    os.close(fd)
    shutil.copy2(input_path, tmp)
    return tmp


def _cleanup_input(task_self, input_path: str, succeeded: bool) -> None:
    """Delete the original input file only after success or max retries."""
    if succeeded or task_self.request.retries >= task_self.max_retries:
        _safe_remove(input_path)


# --------------------------------------------------------------------------- #
# Throttled Redis updates
# MED-01: capped OrderedDict to prevent unbounded memory growth
# --------------------------------------------------------------------------- #
_MAX_TRACKED_JOBS = 10_000
_last_update_map: OrderedDict = OrderedDict()
_last_update_lock = threading.Lock()


def safe_job_update(job_id: str, data: dict) -> None:
    """Throttle Redis job updates to at most one per second per job."""
    now = time.time()
    with _last_update_lock:
        last = _last_update_map.get(job_id, 0)
        if now - last > 1:
            redis_service.job_update(job_id, data)
            _last_update_map[job_id] = now
            _last_update_map.move_to_end(job_id)
            if len(_last_update_map) > _MAX_TRACKED_JOBS:
                _last_update_map.popitem(last=False)


# --------------------------------------------------------------------------- #
# PDF validation helpers
# --------------------------------------------------------------------------- #
def validate_pdf(path: str) -> bool:
    try:
        doc = fitz.open(path)
        doc.close()
        return True
    except Exception:
        return False


def safe_open_pdf(path: str) -> "fitz.Document":
    return fitz.open(path)


# --------------------------------------------------------------------------- #
# Throttled progress helpers
# MED-02: both use timestamp-based throttle (not percentage-based)
# --------------------------------------------------------------------------- #
_progress_last_update: OrderedDict = OrderedDict()


def _throttled_progress(page_num: int, total: int, job_id: str) -> None:
    """Update Redis progress no more than once per second."""
    now = time.time()
    with _last_update_lock:
        last = _progress_last_update.get(job_id, 0)
        if now - last >= 1:
            pct = int((page_num + 1) / total * 100)
            safe_job_update(
                job_id,
                {
                    "progress": str(pct),
                    "current_page": str(page_num + 1),
                    "total_pages": str(total),
                },
            )
            _progress_last_update[job_id] = now
            _progress_last_update.move_to_end(job_id)
            if len(_progress_last_update) > _MAX_TRACKED_JOBS:
                _progress_last_update.popitem(last=False)


# Single-pass throttled progress — uses the same timestamp logic (HIGH-02 fix)
_throttled_progress_single = _throttled_progress


# --------------------------------------------------------------------------- #
# Per-page OCR helper
# --------------------------------------------------------------------------- #
def _ocr_single_pdf(
    input_path: str,
    output_path: str,
    lang: str,
    dpi: int,
    psm: int,
    oem: int,
    min_confidence: int = 30,
    job_id: str = "",
    progress_callback=None,
) -> None:
    """
    OCR every page in ``input_path``; write a searchable PDF to ``output_path``.

    CRIT-04: img is initialised to None before the try block.
    CRIT-05: pytesseract.image_to_data called without unsupported `timeout=`.
    CRIT-06: out_doc.save() is only called on the SUCCESS path via a temp file
             that is atomically renamed; a partial run never lands at output_path.
    MED-02:  `del pix` used instead of pix.close() for fitz compatibility.
    MED-03:  skip-text threshold is >20 chars (was truthy, fooled by 1 char).
    """
    if not TESSERACT_AVAILABLE:
        raise RuntimeError("pytesseract not installed")
    if not FITZ_AVAILABLE:
        raise RuntimeError("PyMuPDF (fitz) not installed")

    dpi = min(dpi, 600)  # clamp against OOM

    src_doc = fitz.open(input_path)
    out_doc = fitz.open()
    total_pages = len(src_doc)

    try:
        for page_num, src_page in enumerate(src_doc):
            # MED-03: only skip if page already has meaningful text (>20 chars)
            existing_text = src_page.get_text().strip()
            if len(existing_text) > 20:
                new_page = out_doc.new_page(
                    width=src_page.rect.width,
                    height=src_page.rect.height,
                )
                new_page.show_pdf_page(
                    fitz.Rect(0, 0, src_page.rect.width, src_page.rect.height),
                    src_doc,
                    page_num,
                )
                if progress_callback:
                    progress_callback(page_num, total_pages)
                continue

            pw, ph = src_page.rect.width, src_page.rect.height
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pix = src_page.get_pixmap(matrix=mat, alpha=False, colorspace=fitz.csGRAY)

            try:
                img_bytes = pix.tobytes("png")
                img_sx = pw / pix.width
                img_sy = ph / pix.height
            except Exception as e:
                log.warning(f"Page {page_num + 1} pixmap error: {e}")
                del pix  # MED-02: use del instead of pix.close()
                continue
            finally:
                # Always release the pixmap memory whether or not tobytes succeeded
                try:
                    del pix
                except Exception:
                    pass

            # CRIT-04: initialise img to None so finally block is safe
            img = None
            try:
                img = Image.open(io.BytesIO(img_bytes))

                tess_cfg = f"--psm {psm} --oem {oem}"
                # CRIT-05: timeout= removed — not a valid pytesseract kwarg
                ocr_data = pytesseract.image_to_data(
                    img,
                    lang=lang,
                    output_type=TesseractOutput.DICT,
                    config=tess_cfg,
                )
            finally:
                if img is not None:
                    img.close()

            new_page = out_doc.new_page(width=pw, height=ph)

            for word_str, conf_str, x0, y0, wd, ht in zip(
                ocr_data.get("text", []),
                ocr_data.get("conf", []),
                ocr_data.get("left", []),
                ocr_data.get("top", []),
                ocr_data.get("width", []),
                ocr_data.get("height", []),
            ):
                word = (word_str or "").strip()
                try:
                    conf = int(conf_str)
                except (ValueError, TypeError):
                    conf = 0

                if not word or conf < min_confidence:
                    continue

                x0_f = float(x0) * img_sx
                y1_f = (float(y0) + float(ht)) * img_sy
                fs = max(4.0, float(ht) * img_sy * 0.85)

                new_page.insert_text(
                    (x0_f, y1_f),
                    word + " ",
                    fontsize=fs,
                    fontname="helv",
                    color=(0, 0, 0),
                    render_mode=3,
                    overlay=True,
                )

            if progress_callback:
                progress_callback(page_num, total_pages)

        # CRIT-06: only save to output on full success via atomic rename
        tmp_out = output_path + ".ocr_tmp"
        out_doc.save(tmp_out, deflate=True, garbage=2)
        os.replace(tmp_out, output_path)

    finally:
        out_doc.close()
        src_doc.close()


# --------------------------------------------------------------------------- #
# Main OCR Celery task
# --------------------------------------------------------------------------- #
@celery_app.task(
    bind=True,
    max_retries=2,
    name="pdfwala.tasks.ocr_tasks.ocr_pdf_task",
    queue="slow",
    time_limit=3600,
    soft_time_limit=3300,
    acks_late=True,
    reject_on_worker_lost=True,
)
def ocr_pdf_task(
    self,
    input_path: str,
    output_path: str,
    job_id: str,
    lang: str = "eng",
    dpi: int = 300,
    psm: int = 3,
    oem: int = 3,
    min_confidence: int = 30,
):
    """
    Async OCR: rasterise each page, run Tesseract, overlay invisible text.
    Large PDFs (> OCR_CHUNK_THRESHOLD pages) are processed in parallel chunks.
    """
    start_time = time.time()

    # ---- Non-retryable infrastructure checks --------------------------------
    if not TESSERACT_AVAILABLE:
        safe_job_update(job_id, {"status": "failed",
                                 "error": "pytesseract is not installed on this worker"})
        _safe_remove(input_path)
        raise RuntimeError("pytesseract not installed — task will not be retried")

    if not FITZ_AVAILABLE:
        safe_job_update(job_id, {"status": "failed",
                                 "error": "PyMuPDF (fitz) is not installed on this worker"})
        _safe_remove(input_path)
        raise RuntimeError("PyMuPDF not installed — task will not be retried")

    # ---- Language validation -------------------------------------------------
    try:
        _validate_lang(lang)
    except ValueError as lang_ex:
        safe_job_update(job_id, {
            "status": "failed",
            "error": str(lang_ex),
            "available_langs": ", ".join(sorted(_AVAILABLE_LANGS)),
        })
        _safe_remove(input_path)
        raise

    # ---- Disk-space check ---------------------------------------------------
    check_disk_space(MIN_DISK_SPACE_MB, job_id=job_id, path=output_path)

    # ---- Input file size guard ----------------------------------------------
    if os.path.getsize(input_path) > MAX_PDF_SIZE_BYTES:
        raise UserError(
            f"Input PDF exceeds maximum size limit of "
            f"{MAX_PDF_SIZE_BYTES / (1024 * 1024):.0f} MB"
        )

    # ---- Copy input to temp (isolation from concurrent workers) -------------
    tmp_input = _copy_to_temp(input_path)

    succeeded = False
    try:
        safe_job_update(job_id, {"status": "processing"})

        doc_check = fitz.open(tmp_input)
        total_pages = len(doc_check)
        doc_check.close()
        safe_job_update(job_id, {"total_pages": str(total_pages)})

        if total_pages > MAX_PDF_PAGES:
            raise UserError(f"PDF exceeds maximum page limit of {MAX_PDF_PAGES}")

        # ---- Chunked vs single-pass -----------------------------------------
        if total_pages > OCR_CHUNK_THRESHOLD:
            log.info(
                f"ocr_pdf_task {job_id}: {total_pages} pages — chunked "
                f"(chunk={OCR_CHUNK_PAGES}, workers={OCR_MAX_WORKERS})"
            )

            def _process_ocr_chunk(chunk_path: str, chunk_idx: int) -> str:
                # MED-04: write chunk output to Config.TEMP_FOLDER, not /tmp
                chunk_out = os.path.join(
                    Config.TEMP_FOLDER,
                    f"ocr_chunk_{job_id}_{chunk_idx}_out.pdf",
                )
                _ocr_single_pdf(
                    chunk_path, chunk_out,
                    lang, dpi, psm, oem, min_confidence, job_id,
                    progress_callback=lambda p, t: _throttled_progress(p, t, job_id),
                )
                return chunk_out

            success = chunked_pdf_processor(
                input_path=tmp_input,
                output_path=output_path,
                job_id=job_id,
                total_pages=total_pages,
                chunk_size=OCR_CHUNK_PAGES,
                max_workers=OCR_MAX_WORKERS,
                process_chunk_func=_process_ocr_chunk,
                merge_func=merge_pdf_chunks,
                redis_service=redis_service,
                tool_name="OCR",
                report_progress=True,
                chunk_retry=1,
            )

            # HIGH-01: check success BEFORE output existence check
            if not success:
                raise RuntimeError("Chunked OCR processing returned failure")

        else:
            log.info(f"ocr_pdf_task {job_id}: {total_pages} pages — single-pass")
            # HIGH-02: use module-level timestamp-throttled version (no local redef)
            _ocr_single_pdf(
                tmp_input, output_path,
                lang, dpi, psm, oem, min_confidence, job_id,
                progress_callback=lambda p, t: _throttled_progress_single(p, t, job_id),
            )
            success = True

        # ---- Output validation ----------------------------------------------
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError("Output file missing or empty after OCR")

        if not validate_pdf(output_path):
            raise RuntimeError("Corrupted output PDF after OCR")

        succeeded = True

        safe_job_update(job_id, {
            "status": "completed",
            "progress": "100",
            "output_path": output_path,
            "completed_at": get_timestamp(),
            "min_confidence": str(min_confidence),
            "page_count": str(total_pages),
        })
        log_structured(
            "INFO", "ocr_pdf_task completed",
            job_id=job_id,
            duration=round(time.time() - start_time, 2),
            page_count=total_pages,
            min_confidence=min_confidence,
        )
        return {"status": "completed", "output": output_path}

    except Exception as ex:
        log.error(f"ocr_pdf_task {job_id}: {ex}")
        safe_job_update(job_id, {"status": "failed", "error": str(ex)})
        raise

    finally:
        _safe_remove(tmp_input)
        _cleanup_input(self, input_path, succeeded)
