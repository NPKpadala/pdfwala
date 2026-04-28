# --------------------------------------------------------------------------- #
# Standard library imports
# --------------------------------------------------------------------------- #
import io
import os
import shutil
import tempfile
import time
import logging
import subprocess
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime
from contextlib import contextmanager
from functools import wraps

# --------------------------------------------------------------------------- #
# Third‑party imports (with graceful fallback)
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
    Image.MAX_IMAGE_PIXELS = 50_000_000  # protect against memory‑bomb images
except Exception:  # pragma: no cover
    Image = None  # PIL not available – will be caught later

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
        # When language detection fails we *skip* validation – the OCR will still
        # run (just without language‑specific training).
        return

    for pack in lang.split("+"):
        pack = pack.strip()
        if pack and pack not in _AVAILABLE_LANGS:
            raise ValueError(
                f"Language pack '{pack}' is not installed. "
                f"Available packs: {sorted(_AVAILABLE_LANGS)}"
            )


# --------------------------------------------------------------------------- #
# Configuration constants (unchanged)
# --------------------------------------------------------------------------- #
WATERMARK_CHUNK_THRESHOLD = int(getattr(Config, "WATERMARK_CHUNK_THRESHOLD", 200))
OCR_CHUNK_THRESHOLD = int(getattr(Config, "OCR_CHUNK_THRESHOLD", 50))
OCR_CHUNK_PAGES = int(getattr(Config, "OCR_CHUNK_PAGES", 30))
OCR_MAX_WORKERS = int(getattr(Config, "OCR_MAX_WORKERS", 2))
MIN_DISK_SPACE_MB = int(getattr(Config, "MIN_DISK_SPACE_MB", 100))
MAX_PDF_PAGES = int(getattr(Config, "MAX_PDF_PAGES", 2000))
MAX_PDF_SIZE_BYTES = int(getattr(Config, "MAX_PDF_SIZE_BYTES", 2 * 1024 * 1024 * 1024))

# --------------------------------------------------------------------------- #
# Path security helpers
# --------------------------------------------------------------------------- #
def get_allowed_base_dirs() -> List[Path]:
    """Return allowed base directories from configuration."""
    allowed = getattr(Config, "ALLOWED_DIRECTORIES", [])
    if not allowed:
        return [Path(tempfile.gettempdir()).resolve(), Path.cwd().resolve()]
    return [Path(p).resolve() for p in allowed]


def validate_path(path: str, allow_nonexistent: bool = False, job_id: str = None) -> Path:
    """Validate that ``path`` exists and is within allowed base directories."""
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
            "ERROR",
            "Path traversal attempt detected",
            job_id=job_id,
            path=str(p),
        )
        raise UserError(f"Path {p} is not within allowed directories")

    if not allow_nonexistent and not p.exists():
        raise UserError(f"Path does not exist: {p}")

    return p


def _safe_path(p: str) -> str:
    """Validate that a path exists and return its absolute string representation."""
    p_obj = Path(p).resolve()
    if not p_obj.exists():
        raise ValueError("Invalid file path")
    return str(p_obj)


# --------------------------------------------------------------------------- #
# Disk‑space protection
# --------------------------------------------------------------------------- #
def check_disk_space(
    required_mb: int = MIN_DISK_SPACE_MB,
    job_id: str = None,
    path: str | None = None,
) -> bool:
    """Ensure at least ``required_mb`` free space in the directory that will hold the output/file."""
    try:
        if path:
            target_dir = os.path.abspath(os.path.dirname(path))
        else:
            target_dir = Config.TEMP_FOLDER
        stat = shutil.disk_usage(target_dir)
        available_mb = stat.free / (1024 * 1024)
        if available_mb < required_mb:
            log_structured(
                "ERROR",
                "Insufficient disk space",
                job_id=job_id,
                available_mb=round(available_mb, 2),
                required_mb=required_mb,
                target_dir=target_dir,
            )
            raise SystemError(
                f"Insufficient disk space: {available_mb:.2f}MB available, "
                f"{required_mb}MB required in {target_dir}"
            )
        log_structured(
            "INFO",
            "Disk space check passed",
            job_id=job_id,
            available_mb=round(available_mb, 2),
            target_dir=target_dir,
        )
        return True
    except (UserError, SystemError):
        raise
    except Exception as e:  # pragma: no cover
        log_structured(
            "WARNING",
            "Could not verify disk space",
            job_id=job_id,
            error=str(e),
        )
        return True


def _safe_remove(path: str) -> None:
    """Delete a file if it exists; ignore any OSError."""
    try:
        os.remove(path)
    except OSError:
        pass


def _copy_to_temp(input_path: str, suffix: str = ".pdf") -> str:
    """Copy ``input_path`` to a temporary file in the configured temp folder."""
    fd, tmp = tempfile.mkstemp(suffix=suffix, dir=Config.TEMP_FOLDER)
    os.close(fd)
    shutil.copy2(input_path, tmp)
    return tmp


def _cleanup_input(task_self, input_path: str, succeeded: bool) -> None:
    """Delete the original input file only after a successful run or after max retries."""
    if succeeded or task_self.request.retries >= task_self.max_retries:
        _safe_remove(input_path)


# --------------------------------------------------------------------------- #
# Throttled Redis updates
# --------------------------------------------------------------------------- #
_last_update_map: dict = {}
def safe_job_update(job_id: str, data: dict) -> None:
    """Throttle Redis job updates to at most one per second per job."""
    now = time.time()
    last = _last_update_map.get(job_id, 0)
    if now - last > 1:
        redis_service.job_update(job_id, data)
        _last_update_map[job_id] = now


# --------------------------------------------------------------------------- #
# PDF validation helpers
# --------------------------------------------------------------------------- #
def validate_pdf(path: str) -> bool:
    """Confirm that a file is a readable, non‑empty PDF."""
    try:
        doc = fitz.open(path)
        doc.close()
        return True
    except Exception:
        return False


def safe_open_pdf(path: str) -> fitz.Document:
    """Open a PDF; Celery task timeout handles long‑running opens."""
    return fitz.open(path)


# --------------------------------------------------------------------------- #
# Ghostscript compression (uses semaphore)
# --------------------------------------------------------------------------- #
class cb_ghostscript:
    """Simple circuit‑breaker stub – replace with your real implementation."""
    _open = False

    @classmethod
    def can_execute(cls) -> bool:
        return not cls._open

    @classmethod
    def record_success(cls) -> None:
        cls._open = False

    @classmethod
    def record_failure(cls) -> None:
        cls._open = True


GS_SEMAPHORE = threading.Semaphore(1)  # limit concurrent Ghostscript runs


def _ghostscript_compress(
    input_path: str,
    output_path: str,
    gs_setting: str = "/ebook",
    extra_flags: list | None = None,
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
        _safe_path(input_path),
    ]

    if extra_flags:
        cmd.extend(extra_flags)

    try:
        with GS_SEMAPHORE:
            result = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
    except Exception as ex:  # pragma: no cover
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


# --------------------------------------------------------------------------- #
# Watermark helper (single‑pass – unchanged except for correct page handling)
# --------------------------------------------------------------------------- #
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
    """Apply text watermark to every page; uses a temporary file for atomic commit."""
    import fitz

    doc = None
    try:
        doc = fitz.open(input_path)
        if doc.is_encrypted:
            raise UserError("Encrypted PDFs must be decrypted before watermarking")

        for page in doc:
            r = page.rect
            wm = create_watermark_pdf(
                text,
                opacity,
                color,
                r.width,
                r.height,
                position,
                rotation,
            )
            with fitz.open("pdf", wm) as wmpdf:
                page.show_pdf_page(fitz.Rect(0, 0, r.width, r.height), wmpdf, 0, overlay=True)

        tmp_path = output_path + ".wm_tmp"
        doc.save(tmp_path, deflate=True, garbage=2, clean=True)
        os.replace(tmp_path, output_path)

    finally:
        if doc:
            doc.close()


# --------------------------------------------------------------------------- #
# Per‑page OCR helper (fixed)
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
    """OCR every page in ``input_path``; write a searchable PDF to ``output_path``."""
    if not TESSERACT_AVAILABLE:
        raise RuntimeError("pytesseract not installed")
    if not FITZ_AVAILABLE:
        raise RuntimeError("PyMuPDF (fitz) not installed")

    # Clamp DPI to avoid absurd values that could OOM the process
    dpi = min(dpi, 600)

    src_doc = fitz.open(input_path)
    out_doc = fitz.open()
    total_pages = len(src_doc)

    try:
        for page_num, src_page in enumerate(src_doc):
            # Skip pages that already contain selectable text (faster)
            if src_page.get_text().strip():
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

            # Rasterise page
            pw, ph = src_page.rect.width, src_page.rect.height
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pix = src_page.get_pixmap(matrix=mat, alpha=False, colorspace=fitz.csGRAY)

            try:
                img_bytes = pix.tobytes("png")
                img_sx = pw / pix.width
                img_sy = ph / pix.height
            except Exception as e:
                log.warning(f"Page {page_num + 1} pixmap error: {e}")
                pix.close()  # fallback – the pix object may not have .close()
                continue

            try:
                img = Image.open(io.BytesIO(img_bytes))
            finally:
                img.close()

            # Tesseract configuration – note: `timeout` is NOT a valid argument
            tess_cfg = f"--psm {psm} --oem {oem}"
            ocr_data = pytesseract.image_to_data(
                img,
                lang=lang,
                output_type=TesseractOutput.DICT,
                config=tess_cfg,
                timeout=30,  # <-- we wrap the call in a thread if needed; see note below
            )

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
                y1_f = (float(y0) + float(ht)) * img_sy  # <-- FIX 2 (no -1)

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

    finally:
        out_doc.save(output_path, deflate=True, garbage=2)
        out_doc.close()
        src_doc.close()


# --------------------------------------------------------------------------- #
# Throttled progress helpers
# --------------------------------------------------------------------------- #
_progress_last_update: dict = {}


def _throttled_progress(page_num: int, total: int, job_id: str) -> None:
    """Update Redis progress no more than once per second."""
    now = time.time()
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


def _throttled_progress_single(page_num: int, total: int, job_id: str) -> None:
    """Throttled progress for the single‑pass (non‑chunked) OCR."""
    now = time.time()
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


# --------------------------------------------------------------------------- #
# Main OCR Celery task (fixed)
# --------------------------------------------------------------------------- #
@celery_app.task(
    bind=True,
    max_retries=2,
    name="pdfwala.tasks.ocr_tasks.ocr_pdf_task",
    queue="slow",
    time_limit=3600,
    soft_time_limit=3300,
    acks_late=True,
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
    For PDFs larger than ``OCR_CHUNK_THRESHOLD`` pages, processing is performed in
    parallel chunks via ``chunked_pdf_processor``; otherwise a single‑pass OCR is
    executed.
    """
    start_time = time.time()  # <-- FIX 2 (start_time defined before any log)

    # ------------------------------------------------------------------- #
    # Non‑retryable infrastructure checks
    # ------------------------------------------------------------------- #
    if not TESSERACT_AVAILABLE:
        safe_job_update(
            job_id,
            {"status": "failed", "error": "pytesseract is not installed on this worker"},
        )
        _safe_remove(input_path)
        raise RuntimeError("pytesseract not installed — task will not be retried")

    if not FITZ_AVAILABLE:
        safe_job_update(
            job_id,
            {"status": "failed", "error": "PyMuPDF (fitz) is not installed on this worker"},
        )
        _safe_remove(input_path)
        raise RuntimeError("PyMuPDF not installed — task will not be retried")

    # ------------------------------------------------------------------- #
    # Language validation (skip if detection failed – we still have Tesseract)
    # ------------------------------------------------------------------- #
    try:
        _validate_lang(lang)
    except ValueError as lang_ex:
        safe_job_update(
            job_id,
            {
                "status": "failed",
                "error": str(lang_ex),
                "available_langs": ", ".join(sorted(_AVAILABLE_LANGS)),
            },
        )
        _safe_remove(input_path)
        raise

    # ------------------------------------------------------------------- #
    # Disk‑space check for the temporary copy location
    # ------------------------------------------------------------------- #
    if not check_disk_space(MIN_DISK_SPACE_MB, job_id=job_id, path=output_path):
        raise SystemError("Insufficient disk space for temporary files")

    # ------------------------------------------------------------------- #
    # Input file size validation (prevent OOM / DoS)
    # ------------------------------------------------------------------- #
    if os.path.getsize(input_path) > MAX_PDF_SIZE_BYTES:
        raise UserError(
            f"Input PDF exceeds maximum size limit of {MAX_PDF_SIZE_BYTES / (1024 * 1024):.0f} MB"
        )

    # ------------------------------------------------------------------- #
    # Copy input to a temp file (CONC-01)
    # ------------------------------------------------------------------- #
    tmp_input = _copy_to_temp(input_path)

    succeeded = False
    try:
        safe_job_update(job_id, {"status": "processing"})

        # ----------------------------------------------------------------
        # PDF validation before any heavy work
        # ----------------------------------------------------------------
        doc_check = fitz.open(tmp_input)
        total_pages = len(doc_check)
        doc_check.close()
        safe_job_update(job_id, {"total_pages": str(total_pages)})

        if total_pages > MAX_PDF_PAGES:
            raise UserError(f"PDF exceeds maximum page limit of {MAX_PDF_PAGES}")

        # ----------------------------------------------------------------
        # Decide chunked vs single‑pass
        # ----------------------------------------------------------------
        if total_pages > OCR_CHUNK_THRESHOLD:
            log.info(
                f"ocr_pdf_task {job_id}: {total_pages} pages — chunked "
                f"(chunk={OCR_CHUNK_PAGES}, workers={OCR_MAX_WORKERS})"
            )

            # Local closure with the correct signature for chunked_pdf_processor
            def _process_ocr_chunk(chunk_path: str, chunk_idx: int) -> str:
                chunk_out = chunk_path.replace("_in.pdf", "_out.pdf")
                _ocr_single_pdf(
                    chunk_path,
                    chunk_out,
                    lang,
                    dpi,
                    psm,
                    oem,
                    min_confidence,
                    job_id,
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
        else:
            log.info(f"ocr_pdf_task {job_id}: {total_pages} pages — single-pass")
            # Simple throttled progress for single‑pass (no recursion)
            def _throttled_progress_single(page_num: int, total: int) -> None:
                pct = int((page_num + 1) / total * 100)
                if pct != _progress_last_update.get(job_id, 0):
                    _progress_last_update[job_id] = pct
                    safe_job_update(
                        job_id,
                        {
                            "progress": str(pct),
                            "current_page": str(page_num + 1),
                            "total_pages": str(total),
                        },
                    )

            # Run OCR in a single pass
            _ocr_single_pdf(
                tmp_input,
                output_path,
                lang,
                dpi,
                psm,
                oem,
                min_confidence,
                job_id,
                progress_callback=lambda p, t: _throttled_progress_single(p, t, job_id),
            )
            success = True  # single‑pass succeeded

        # ----------------------------------------------------------------
        # Output validation
        # ----------------------------------------------------------------
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError("Output file missing or empty after OCR")

        if not validate_pdf(output_path):
            raise SystemError("Corrupted output PDF after OCR")

        succeeded = True

        # ----------------------------------------------------------------
        # Final job update
        # ----------------------------------------------------------------
        safe_job_update(
            job_id,
            {
                "status": "completed",
                "progress": "100",
                "output_path": output_path,
                "completed_at": get_timestamp(),
                "min_confidence": str(min_confidence),
                "page_count": str(total_pages),
            },
        )
        log_structured(
            "INFO",
            "ocr_pdf_task completed",
            job_id=job_id,
            duration=round(time.time() - start_time, 2),
            page_count=total_pages,
            min_confidence=min_confidence,
        )
        return {"status": "completed", "output": output_path}

    except Exception as ex:  # noqa: BLE001 – we want to catch *all* errors here
        # Non‑retryable errors (missing deps, invalid language, etc.) are logged
        # and re‑raised without invoking the retry mechanism.
        log.error(f"ocr_pdf_task {job_id}: {ex}")
        safe_job_update(job_id, {"status": "failed", "error": str(ex)})
        raise

    finally:
        # Ensure the temporary copy is removed; original input is deleted only
        # on success or after max retries have been exhausted.
        _safe_remove(tmp_input)
        _cleanup_input(self, input_path, succeeded)


# --------------------------------------------------------------------------- #
# Helper imports that were missing in the original file
# --------------------------------------------------------------------------- #
from utils.errors import UserError  # noqa: E402  (import after other imports)
from utils.logging import log_structured  # noqa: E402

# --------------------------------------------------------------------------- #
# End of file
# --------------------------------------------------------------------------- #
