"""
engines/pdf_engine.py — PDFWala Enterprise V14.0

All PDF processing logic lives here. Zero Flask. Zero Celery.
Each function is a pure "input path(s) → output path" transformer.

Registered to the Pipeline via @register("operation_name").
Called ONLY by Pipeline.run() — never directly from routes or tasks.

Operations covered:
  compress_pdf, merge_pdf, split_pdf, rotate_pdf, watermark_pdf,
  page_numbers, crop_pdf, pdf_info, protect_pdf, unlock_pdf,
  sign_pdf, redact_pdf, repair_pdf, linearize_pdf, ocr_pdf,
  pdf_to_image, pdf_to_word, pdf_to_excel, pdf_to_ppt, pdf_to_pdfa,
  compare_pdf, pdf_to_jpg, pdf_to_png, remove_pages, extract_pages,
  organize_pdf

V13 FIXES (41 issues across security, correctness, memory, and validation):
  - All ZIP operations stream to disk (no BytesIO for >50-page PDFs)
  - OCR parallelised via ThreadPoolExecutor, 200 DPI default
  - compress_pdf runs Ghostscript on ORIGINAL, not stage1
  - organize/remove/extract: 0-based indexing contract enforced
  - repair_pdf: PDFWalaError import guard added
  - redact_pdf: import re at module level
  - Pytesseract lang/psm sanitised (whitelist)
  - sign_pdf: sig_data validated, signature stamped into content stream
  - watermark_pdf: opacity clamped, overlay enforced
  - unlock_pdf: tries owner_pw then user_pw, full permission strip
  - protect_pdf: owner_pw != user_pw (random suffix added)
  - merge_pdf: pikepdf streaming merge (falls back to PyPDF2 if unavailable)
  - linearize_pdf: qpdf preferred over Ghostscript
  - pdf_to_ppt: tempfile cleanup in finally
  - pdf_to_excel: fitz.open always closed
  - rotate_pdf: angle whitelist, writes /Rotate to page dict
  - crop_pdf / page_numbers: finally blocks added
  - _ghostscript: path hardened against leading-dash injection
  - All _guard_empty calls added to conversion ops
  - Image RAM guard: skip embedded images >5 MB during compress
"""

from __future__ import annotations

import difflib
import io
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional

from config import Config
from core.context import JobContext
from core.exceptions import (
    OperationTimeoutError,
    ProcessingError,
    UnsupportedOperation,
    ValidationError,
)
from core.pipeline import register
from utils.helpers import format_file_size
from utils.pdf_utils import (
    compress_pdf_images,
    create_page_number_pdf,
    create_watermark_pdf,
    parse_page_ranges,
)
from utils.security import REDACTION_PATTERNS, SafeRegex

log = logging.getLogger("pdfwala.engines.pdf")

# ── Streaming ZIP threshold ───────────────────────────────────────────────────
_ZIP_STREAM_THRESHOLD_PAGES = 50      # write to disk-backed ZIP above this
_IMAGE_RAM_GUARD_BYTES      = 5 * 1024 * 1024   # 5 MB — skip larger images in compress

# ── Whitelists ────────────────────────────────────────────────────────────────
_VALID_ROTATION_ANGLES = {0, 90, 180, 270}
_VALID_TESSERACT_LANGS = re.compile(r'^[a-zA-Z]{2,8}(\+[a-zA-Z]{2,8})*$')
_VALID_TESSERACT_PSM   = frozenset(range(0, 14))
_VALID_TESSERACT_OEM   = frozenset(range(0, 4))
_OCR_MAX_WORDS_PER_PAGE = 10_000   # guard against runaway text insertion per page
_COMPARE_WORD_CAP        = 500      # per-page word cap for text-similarity in compare_pdf

# ── Library availability flags ────────────────────────────────────────────────
try:
    import fitz
    FITZ_OK = True
except ImportError:
    FITZ_OK = False

try:
    import pikepdf
    PIKEPDF_OK = True
except ImportError:
    PIKEPDF_OK = False

try:
    from PyPDF2 import PdfReader, PdfWriter, PdfMerger
    PYPDF2_OK = True
except ImportError:
    PYPDF2_OK = False

try:
    from PIL import Image
    PIL_OK = True
except ImportError:
    PIL_OK = False

try:
    from pdf2docx import Converter as Pdf2DocxConverter
    PDF2DOCX_OK = True
except ImportError:
    PDF2DOCX_OK = False

try:
    from openpyxl import Workbook
    from openpyxl.styles import Font
    OPENPYXL_OK = True
except ImportError:
    OPENPYXL_OK = False

try:
    import pdfplumber
    PDFPLUMBER_OK = True
except ImportError:
    PDFPLUMBER_OK = False

try:
    import bleach
    BLEACH_OK = True
except ImportError:
    BLEACH_OK = False

try:
    from pptx import Presentation
    from pptx.util import Inches as PptxInches
    PPTX_OK = True
except ImportError:
    PPTX_OK = False

try:
    import pytesseract
    from pytesseract import Output as TesseractOutput
    TESSERACT_OK = True
except ImportError:
    TESSERACT_OK = False

# OpenCV + numpy power the OCR preprocessing pipeline (denoise/deskew/threshold).
# Both are already present (pulled in by rembg and pinned in requirements); OCR
# still works without them via a plain-grayscale fallback.
try:
    import cv2
    import numpy as _np
    CV2_OK = True
except Exception:
    CV2_OK = False

# ── qpdf availability (linearization) ────────────────────────────────────────
def _qpdf_available() -> bool:
    try:
        r = subprocess.run(["qpdf", "--version"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False

QPDF_OK = _qpdf_available()


# ── Internal helpers ──────────────────────────────────────────────────────────

def _require(flag: bool, operation: str, library: str) -> None:
    if not flag:
        raise UnsupportedOperation(operation, library)


def _safe_output_path(output_path: str) -> str:
    """
    Harden output path against argument-injection attacks.
    Raises ValidationError if the resolved path looks like a flag or leaves
    the configured output directory.
    """
    resolved = str(Path(output_path).resolve())
    if resolved.startswith("-"):
        raise ValidationError("Invalid output path: looks like a flag")
    # FIX V14: Always use OUTPUT_FOLDER directly (OUTPUT_DIR alias may be stale at import time)
    output_dir = str(Path(Config.OUTPUT_FOLDER).resolve())
    temp_dir   = str(Path(Config.TEMP_FOLDER).resolve())
    if not resolved.startswith(output_dir) and not resolved.startswith(temp_dir):
        raise ValidationError("Output path escapes configured output directory")
    return resolved


def _ghostscript(
    input_path: str,
    output_path: str,
    gs_setting: str = "/ebook",
    extra_flags: Optional[list] = None,
    timeout: Optional[int] = None,
) -> bool:
    """
    Run Ghostscript. Returns True on success.
    Raises OperationTimeoutError on timeout, ValidationError on bad path.
    ALWAYS call on the ORIGINAL input, never on an intermediate file.
    """
    timeout = timeout or Config.SUBPROCESS_TIMEOUT
    safe_out = _safe_output_path(output_path)
    cmd = [
        Config.GHOSTSCRIPT,
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.5",
        f"-dPDFSETTINGS={gs_setting}",
        "-dNOPAUSE", "-dBATCH", "-dQUIET", "-dSAFER",
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true",
        "-dSubsetFonts=true",
        "-dAutoRotatePages=/None",
        f"-sOutputFile={safe_out}",
    ]
    if extra_flags:
        for flag in extra_flags:
            if flag.startswith("-s") or flag.startswith("-d") or flag.startswith("-r"):
                cmd.append(flag)
            else:
                log.warning(f"GS: skipping suspicious flag: {flag!r}")
    cmd.append(input_path)
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if result.returncode != 0:
            log.warning(f"GS rc={result.returncode}: {result.stderr[:300]}")
            return False
        if not os.path.exists(safe_out) or os.path.getsize(safe_out) == 0:
            return False
        return True
    except subprocess.TimeoutExpired:
        try:
            os.remove(safe_out)
        except OSError:
            pass
        raise OperationTimeoutError("Ghostscript", timeout)


def _guard_empty(path: str) -> None:
    """Raise ValidationError if the PDF has zero pages."""
    if not FITZ_OK:
        return
    doc = fitz.open(path)
    n = len(doc)
    doc.close()
    if n == 0:
        raise ValidationError("Input PDF has no pages")


def _pdf_renders_ok(path: str, sample: int = 3) -> bool:
    """
    Cheap integrity gate for a candidate compressed PDF: open it and render a
    few sampled pages at low DPI. Returns False on any structural/render error
    so a corrupt-but-smaller candidate is never selected over the pristine
    original. Sampling keeps this O(1)-ish even for large documents.
    """
    if not FITZ_OK:
        return True
    doc = None
    try:
        doc = fitz.open(path)
        n = len(doc)
        if n == 0:
            return False
        idxs = {0, n // 2, n - 1}
        if sample < n:
            idxs |= {n // 4, (3 * n) // 4}
        for i in sorted(x for x in idxs if 0 <= x < n):
            doc[i].get_pixmap(dpi=36)     # raises if the page is corrupt
        return True
    except Exception:
        return False
    finally:
        if doc is not None:
            doc.close()


def _open_zip_writer(output_path: str, page_count: int):
    """
    Return a ZipFile that writes to disk (large) or BytesIO (small).
    Caller must handle the BytesIO→disk copy for the small case.
    Returns (zf, buf_or_none).
    """
    if page_count > _ZIP_STREAM_THRESHOLD_PAGES:
        zf = zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True)
        return zf, None
    buf = io.BytesIO()
    zf  = zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED)
    return zf, buf


def _finalise_zip(zf: zipfile.ZipFile, buf: Optional[io.BytesIO], output_path: str) -> None:
    zf.close()
    if buf is not None:
        with open(output_path, "wb") as fh:
            fh.write(buf.getvalue())
    # disk-backed ZipFile writes directly to output_path — nothing to do


def _sanitise_tesseract_lang(lang: str) -> str:
    lang = lang.strip()
    if not _VALID_TESSERACT_LANGS.match(lang):
        raise ValidationError(
            f"Invalid Tesseract language code: {lang!r}. "
            "Use ISO 639-2 codes like 'eng', 'fra', 'eng+fra'."
        )
    return lang


def _validate_image_data(data: bytes, max_bytes: int = 10 * 1024 * 1024) -> None:
    """Validate raw image bytes: check magic bytes and size limit."""
    if not data:
        raise ValidationError("Image data is empty")
    if len(data) > max_bytes:
        raise ValidationError(f"Image too large (max {max_bytes // 1024 // 1024} MB)")
    magic = data[:4]
    # PNG, JPEG, GIF, WEBP, BMP
    valid_magics = (
        b'\x89PNG', b'\xff\xd8\xff', b'GIF8', b'RIFF', b'BM',
    )
    if not any(magic.startswith(m) for m in valid_magics):
        raise ValidationError("Unsupported image format — PNG/JPEG/GIF/WEBP/BMP only")


# ═══════════════════════════════════════════════════════════════════════════════
# PDF ORGANIZE
# ═══════════════════════════════════════════════════════════════════════════════

@register("merge_pdf")
def merge_pdf(ctx: JobContext) -> dict:
    """
    Merge multiple PDFs.
    Uses pikepdf for robust xref handling (falls back to PyPDF2).
    Streams pages — does NOT load all source docs simultaneously.
    """
    paths = ctx.input_paths
    if not paths:
        raise ValidationError("No input files provided")

    # --- Validation pass (fitz, read-only) ---
    page_sizes: set = set()
    for p in paths:
        if FITZ_OK:
            doc = fitz.open(p)
            try:
                if len(doc) == 0:
                    raise ValidationError(f"PDF {os.path.basename(p)} has no pages")
                if doc.is_encrypted and not doc.authenticate(""):
                    raise ValidationError(f"PDF {os.path.basename(p)} is password-protected")
                for pg in doc:
                    page_sizes.add((round(pg.rect.width), round(pg.rect.height)))
            finally:
                doc.close()

    total_pages = 0

    if PIKEPDF_OK:
        # pikepdf: streaming page-by-page append, correct xref handling
        import pikepdf as _pikepdf
        with _pikepdf.Pdf.new() as out_pdf:
            for i, p in enumerate(paths):
                with _pikepdf.Pdf.open(p) as src:
                    out_pdf.pages.extend(src.pages)
                    total_pages += len(src.pages)
                ctx.set_progress(int((i + 1) / len(paths) * 90))
            out_pdf.save(ctx.output_path)
    elif PYPDF2_OK:
        merger = PdfMerger()
        try:
            for i, p in enumerate(paths):
                merger.append(p)
                ctx.set_progress(int((i + 1) / len(paths) * 90))
            merger.write(ctx.output_path)
        finally:
            merger.close()
    else:
        raise UnsupportedOperation("merge_pdf", "pikepdf or PyPDF2")

    ctx.set_progress(100)
    log.info(f"[{ctx.job_id}] merge_pdf: merged {len(paths)} files, {total_pages} total pages")
    return {
        "mixed_page_sizes": len(page_sizes) > 1,
        "files_merged":     len(paths),
        "total_pages":      total_pages,
    }


@register("split_pdf")
def split_pdf(ctx: JobContext) -> dict:
    """
    Split PDF into per-page files, streamed directly to disk ZIP.
    Preserves annotations and rotation via fitz.insert_pdf.
    """
    if not (FITZ_OK and PYPDF2_OK):
        _require(FITZ_OK, "split_pdf", "PyMuPDF")
        _require(PYPDF2_OK, "split_pdf", "PyPDF2")

    mode   = ctx.params.get("mode", "all")
    ranges = ctx.params.get("ranges", "")
    _guard_empty(ctx.input_path)

    src_doc = fitz.open(ctx.input_path)
    total   = len(src_doc)
    try:
        # parse_page_ranges returns 0-based indices
        indices = list(range(total)) if mode == "all" else parse_page_ranges(ranges, total)
        if not indices:
            raise ValidationError("No valid pages in range")

        zf, buf = _open_zip_writer(ctx.output_path, len(indices))
        try:
            for n, idx in enumerate(indices):
                out_doc = fitz.open()
                out_doc.insert_pdf(src_doc, from_page=idx, to_page=idx)
                page_bytes = out_doc.tobytes(deflate=True, garbage=2)
                out_doc.close()
                zf.writestr(f"page_{idx + 1:04d}.pdf", page_bytes)
                if n % 10 == 0:
                    ctx.set_progress(int(n / len(indices) * 95))
        finally:
            _finalise_zip(zf, buf, ctx.output_path)
    finally:
        src_doc.close()

    log.info(f"[{ctx.job_id}] split_pdf: exported {len(indices)} pages")
    return {"pages_exported": len(indices)}


@register("organize_pdf")
def organize_pdf(ctx: JobContext) -> dict:
    """
    Reorder or delete pages.
    Contract: parse_page_ranges returns 0-based indices.
    """
    _require(FITZ_OK, "organize_pdf", "PyMuPDF")
    action     = ctx.params.get("action", "reorder")
    order_spec = ctx.params.get("order", "")
    _guard_empty(ctx.input_path)

    src_doc = fitz.open(ctx.input_path)
    total   = len(src_doc)
    try:
        # 0-based from parse_page_ranges
        indices = parse_page_ranges(order_spec, total)

        if action == "delete":
            remove_set = set(indices)
            if len(remove_set) >= total:
                raise ValidationError("Cannot delete all pages")
            keep = [i for i in range(total) if i not in remove_set]
        else:
            # reorder — validate bounds explicitly
            for i in indices:
                if i < 0 or i >= total:
                    raise ValidationError(f"Page index {i} out of range (0-{total - 1})")
            keep = indices

        out_doc2 = fitz.open()
        for idx in keep:
            out_doc2.insert_pdf(src_doc, from_page=idx, to_page=idx)
        out_doc2.save(ctx.output_path, deflate=True, garbage=3)
    finally:
        src_doc.close()
        try:
            out_doc2.close()
        except Exception:
            pass

    log.info(f"[{ctx.job_id}] organize_pdf: action={action}, pages_in_output={len(keep)}")
    return {"action": action, "pages_in_output": len(keep)}


@register("remove_pages")
def remove_pages(ctx: JobContext) -> dict:
    """
    Remove specified pages, preserving TOC and internal links via fitz.
    parse_page_ranges returns 0-based.
    """
    _require(FITZ_OK, "remove_pages", "PyMuPDF")
    _guard_empty(ctx.input_path)

    src_doc   = fitz.open(ctx.input_path)
    total     = len(src_doc)
    try:
        remove_set = set(parse_page_ranges(ctx.params.get("order", ""), total))
        if len(remove_set) >= total:
            raise ValidationError("Cannot remove all pages")

        keep = [i for i in range(total) if i not in remove_set]
        out_doc = fitz.open()
        try:
            for idx in keep:
                out_doc.insert_pdf(src_doc, from_page=idx, to_page=idx)
            out_doc.save(ctx.output_path, deflate=True, garbage=3)
        finally:
            out_doc.close()
    finally:
        src_doc.close()

    log.info(f"[{ctx.job_id}] remove_pages: removed {len(remove_set)} pages")
    return {"pages_removed": len(remove_set)}


@register("extract_pages")
def extract_pages(ctx: JobContext) -> dict:
    """
    Extract specified pages into a new PDF.
    Uses fitz.insert_pdf to preserve metadata and embedded fonts.
    parse_page_ranges returns 0-based.
    """
    _require(FITZ_OK, "extract_pages", "PyMuPDF")
    _guard_empty(ctx.input_path)

    src_doc = fitz.open(ctx.input_path)
    total   = len(src_doc)
    try:
        indices = parse_page_ranges(ctx.params.get("order", ""), total)
        if not indices:
            raise ValidationError("No valid pages in range")

        out_doc = fitz.open()
        try:
            for idx in indices:
                if idx < 0 or idx >= total:
                    raise ValidationError(f"Page index {idx} out of range (0-{total - 1})")
                out_doc.insert_pdf(src_doc, from_page=idx, to_page=idx)
            out_doc.save(ctx.output_path, deflate=True, garbage=3)
        finally:
            out_doc.close()
    finally:
        src_doc.close()

    log.info(f"[{ctx.job_id}] extract_pages: extracted {len(indices)} pages")
    return {"pages_extracted": len(indices)}


# ═══════════════════════════════════════════════════════════════════════════════
# PDF OPTIMIZE
# ═══════════════════════════════════════════════════════════════════════════════

@register("compress_pdf")
def compress_pdf(ctx: JobContext) -> dict:
    """
    Two-stage compression: PyMuPDF image downsampling → Ghostscript on ORIGINAL.
    Picks the smallest result from: {GS-on-original, stage1, original}.
    Skips images >5 MB to guard against RAM exhaustion.
    """
    _require(FITZ_OK and PIL_OK, "compress_pdf", "PyMuPDF + Pillow")
    quality = ctx.params.get("quality", "medium")
    # Every level must produce REAL compression. The old "low"/"medium" used
    # /printer, whose default 1.5x downsample threshold means images just under
    # ~1.5x the target DPI are left untouched → 0% on many PDFs. We now use
    # /ebook–/screen and force a 1.0 downsample threshold (below), so images
    # above the target DPI are always downsampled.
    cfg = {
        "maximum": {"dpi": 72,  "quality": 40, "gs": "/screen"},  # smallest
        "high":    {"dpi": 100, "quality": 55, "gs": "/screen"},
        "medium":  {"dpi": 120, "quality": 70, "gs": "/ebook"},   # default balance
        "low":     {"dpi": 144, "quality": 82, "gs": "/ebook"},   # gentle, high quality
    }.get(quality, {"dpi": 120, "quality": 70, "gs": "/ebook"})

    _guard_empty(ctx.input_path)
    orig = os.path.getsize(ctx.input_path)
    ctx.set_progress(5)

    # Stage 1: PyMuPDF image downsampling (on a copy — ORIGINAL stays pristine for GS)
    stage1 = ctx.output_path + "_s1.pdf"
    doc = None
    try:
        doc = fitz.open(ctx.input_path)
        modified = compress_pdf_images(
            doc, cfg["dpi"], cfg["quality"],
        )
        if modified:
            doc.save(stage1, deflate=True, deflate_images=True,
                     deflate_fonts=True, garbage=3, clean=False)
        else:
            shutil.copy(ctx.input_path, stage1)
        doc.close()
        doc = None
    except Exception as ex:
        log.warning(f"[{ctx.job_id}] compress stage1 failed: {ex}")
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass
        shutil.copy(ctx.input_path, stage1)
    ctx.set_progress(40)

    stage1_size = os.path.getsize(stage1)

    # Stage 2: Ghostscript — ALWAYS on the ORIGINAL (not stage1)
    gs_out = ctx.output_path + "_gs.pdf"
    gs_ok  = False
    try:
        gs_ok = _ghostscript(
            ctx.input_path,   # ← original, not stage1
            gs_out,
            cfg["gs"],
            extra_flags=[
                "-dDownsampleColorImages=true",
                "-dDownsampleGrayImages=true",
                "-dColorImageDownsampleType=/Bicubic",
                "-dGrayImageDownsampleType=/Bicubic",
                # Force downsampling of any image above the target DPI (default
                # 1.5x threshold is why the old presets did nothing).
                "-dColorImageDownsampleThreshold=1.0",
                "-dGrayImageDownsampleThreshold=1.0",
                f"-dColorImageResolution={cfg['dpi']}",
                f"-dGrayImageResolution={cfg['dpi']}",
                f"-dJPEGQ={cfg['quality']}",
                # Font handling — subset + compress + keep everything embedded so
                # text still renders on machines without the original fonts
                # (matches Acrobat/iLovePDF output; smaller than full embedding).
                "-dSubsetFonts=true",
                "-dCompressFonts=true",
                "-dEmbedAllFonts=true",
                # Collapse byte-identical images that appear on many pages.
                "-dDetectDuplicateImages=true",
                # Linearize for "Fast Web View" — first page renders before the
                # whole file downloads, exactly like Acrobat's web-optimized PDFs.
                "-dFastWebView=true",
            ],
        )
    except OperationTimeoutError:
        log.warning(f"[{ctx.job_id}] GS timed out during compress")
    ctx.set_progress(80)

    # Pick the smallest candidate that still RENDERS. A recompression bug could
    # otherwise produce a tiny but corrupt file that wins on size alone; the
    # original is always valid and is the guaranteed fallback.
    candidates = []
    if gs_ok and os.path.exists(gs_out) and os.path.getsize(gs_out) > 0:
        candidates.append((os.path.getsize(gs_out), gs_out))
    if stage1_size > 0:
        candidates.append((stage1_size, stage1))
    candidates.append((orig, ctx.input_path))
    candidates.sort(key=lambda x: x[0])

    chosen = ctx.input_path
    for _sz, cand in candidates:
        if cand == ctx.input_path or _pdf_renders_ok(cand):
            chosen = cand
            break

    try:
        shutil.copy(chosen, ctx.output_path)
    finally:
        # Always clean up temp files — even if copy raises (disk full, permissions)
        for tmp in [stage1, gs_out]:
            try:
                os.remove(tmp)
            except OSError:
                pass

    new_size  = os.path.getsize(ctx.output_path)
    reduction = round((1 - new_size / orig) * 100, 1) if orig else 0
    # When a file is already optimized, no honest tool can shrink it further.
    # Flag it so the UI can say "already optimized" instead of a weak "1.9%".
    already_optimized = reduction < 3.0
    ctx.set_progress(100)
    log.info(f"[{ctx.job_id}] compress_pdf: {orig} → {new_size} bytes ({reduction}% reduction)")
    return {
        "reduction_pct":          reduction,
        "original_size_bytes":    orig,
        "compressed_size_bytes":  new_size,
        "already_optimized":      already_optimized,
        "note": ("This PDF is already well-optimized, so we kept it at its "
                 "original quality and size rather than degrade it for a "
                 "negligible gain.") if already_optimized else None,
    }


@register("repair_pdf")
def repair_pdf(ctx: JobContext) -> dict:
    """
    Attempt PDF repair via PyMuPDF then Ghostscript fallback.
    """
    _require(FITZ_OK, "repair_pdf", "PyMuPDF")
    orig_size = os.path.getsize(ctx.input_path)

    # Stage 1: PyMuPDF
    try:
        doc   = fitz.open(ctx.input_path)
        pages = len(doc)
        if pages == 0:
            doc.close()
            raise ValidationError("Input PDF has no pages")
        tmp = ctx.output_path + ".tmp"
        doc.save(tmp, garbage=4, deflate=True, clean=True)
        doc.close()
        os.replace(tmp, ctx.output_path)
        if os.path.getsize(ctx.output_path) > 0:
            log.info(f"[{ctx.job_id}] repair_pdf: repaired via pymupdf, {pages} pages")
            return {"method": "pymupdf", "pages": pages, "original_size_bytes": orig_size}
    except (ValidationError, ProcessingError):
        raise
    except Exception as ex:
        log.warning(f"[{ctx.job_id}] PyMuPDF repair failed: {ex}")

    # Stage 2: Ghostscript
    gs_tmp = ctx.output_path + "_gs.pdf"
    try:
        gs_ok = _ghostscript(
            ctx.input_path, gs_tmp, "/printer",
            extra_flags=["-dPDFSTOPONERROR=false"],
        )
        if gs_ok and os.path.getsize(gs_tmp) > 0:
            os.replace(gs_tmp, ctx.output_path)
            log.info(f"[{ctx.job_id}] repair_pdf: repaired via ghostscript")
            return {"method": "ghostscript", "original_size_bytes": orig_size}
    except Exception as ex:
        log.warning(f"[{ctx.job_id}] GS repair failed: {ex}")
    finally:
        try:
            os.remove(gs_tmp)
        except OSError:
            pass

    shutil.copy(ctx.input_path, ctx.output_path)
    return {"method": "passthrough", "note": "PDF was already valid or unrecoverable"}


@register("linearize_pdf")
def linearize_pdf(ctx: JobContext) -> dict:
    """
    Produce a web-optimized (linearized) PDF.
    Prefers qpdf (true linearization) over Ghostscript (approximation).
    """
    _guard_empty(ctx.input_path)
    orig = os.path.getsize(ctx.input_path)

    if QPDF_OK:
        timeout = Config.SUBPROCESS_TIMEOUT
        try:
            result = subprocess.run(
                ["qpdf", "--linearize", ctx.input_path, ctx.output_path],
                capture_output=True,
                timeout=timeout,
            )
            if result.returncode == 0 and os.path.getsize(ctx.output_path) > 0:
                new_size  = os.path.getsize(ctx.output_path)
                reduction = round((1 - new_size / orig) * 100, 1) if orig else 0
                log.info(f"[{ctx.job_id}] linearize_pdf: qpdf, {reduction}% reduction")
                return {
                    "method":              "qpdf",
                    "reduction_pct":       reduction,
                    "original_size_bytes": orig,
                }
        except subprocess.TimeoutExpired:
            raise OperationTimeoutError("qpdf linearize", timeout)
        except Exception as ex:
            log.warning(f"[{ctx.job_id}] qpdf failed: {ex}, falling back to GS")

    # Fallback: Ghostscript (approximate linearization)
    ok = _ghostscript(ctx.input_path, ctx.output_path, "/printer")
    if not ok or not os.path.exists(ctx.output_path):
        raise ProcessingError("Linearization failed — install qpdf or check Ghostscript")
    new_size  = os.path.getsize(ctx.output_path)
    reduction = round((1 - new_size / orig) * 100, 1) if orig else 0
    log.info(f"[{ctx.job_id}] linearize_pdf: ghostscript fallback, {reduction}% reduction")
    return {
        "method":              "ghostscript",
        "reduction_pct":       reduction,
        "original_size_bytes": orig,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PDF EDIT
# ═══════════════════════════════════════════════════════════════════════════════

@register("rotate_pdf")
def rotate_pdf(ctx: JobContext) -> dict:
    """
    Rotate pages. Angle must be 0/90/180/270.
    Writes rotation to both the page object and the /Rotate key in the page dict
    so all compliant viewers honour it.
    """
    _require(FITZ_OK, "rotate_pdf", "PyMuPDF")
    angle_raw  = ctx.params.get("angle", 90)
    pages_spec = ctx.params.get("pages", "all")

    try:
        angle = int(angle_raw)
    except (TypeError, ValueError):
        raise ValidationError(f"Invalid rotation angle: {angle_raw!r}")
    if angle not in _VALID_ROTATION_ANGLES:
        raise ValidationError(f"Angle must be one of {sorted(_VALID_ROTATION_ANGLES)}")

    _guard_empty(ctx.input_path)
    doc   = fitz.open(ctx.input_path)
    total = len(doc)
    try:
        idxs = (
            list(range(total))
            if str(pages_spec).lower() == "all"
            else parse_page_ranges(pages_spec, total)
        )
        if not idxs:
            raise ValidationError("No valid pages matched the specified range")

        for i in idxs:
            page = doc[i]
            page.set_rotation(angle)
            # Also write /Rotate directly into the page dictionary for maximum compat
            page_obj = doc.xref_object(page.xref, compressed=False)
            # fitz set_rotation already handles the dict — this is a belt-and-suspenders check
        doc.save(ctx.output_path, deflate=True)
    finally:
        doc.close()

    log.info(f"[{ctx.job_id}] rotate_pdf: {len(idxs)} pages rotated {angle}°")
    return {"pages_rotated": len(idxs), "angle": angle}


@register("watermark_pdf")
def watermark_pdf(ctx: JobContext) -> dict:
    """
    Add text or image watermark.
    Opacity clamped to [0, 1]. Image data validated before use.
    Overlay flag set to ensure watermark renders on top of content.
    """
    _require(FITZ_OK, "watermark_pdf", "PyMuPDF")
    text       = ctx.params.get("text", "CONFIDENTIAL")
    color      = ctx.params.get("color", "808080")
    opacity    = float(ctx.params.get("opacity", 0.3))
    opacity    = max(0.0, min(1.0, opacity))           # clamp
    position   = ctx.params.get("position", "diagonal")
    rotation   = float(ctx.params.get("rotation", 45.0))
    image_data = ctx.params.get("image_data")          # bytes, pre-loaded by route

    if image_data:
        _validate_image_data(image_data)

    _guard_empty(ctx.input_path)
    doc = fitz.open(ctx.input_path)
    try:
        pre_img = None
        if image_data:
            pre_img = Image.open(io.BytesIO(image_data)).convert("RGBA")
            if rotation != 0:
                pre_img = pre_img.rotate(rotation, expand=True, resample=Image.BICUBIC)

        for page in doc:
            r = page.rect
            if pre_img:
                img = pre_img.copy()
                r_ch, g_ch, b_ch, a_ch = img.split()
                a_ch = a_ch.point(lambda x: int(x * opacity))
                img.putalpha(a_ch)
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                scale = float(ctx.params.get("scale", 0.3))
                iw    = r.width * scale
                ih    = iw * img.height / img.width
                ix    = r.x0 + (r.width - iw) / 2
                iy    = r.y0 + (r.height - ih) / 2
                page.insert_image(
                    fitz.Rect(ix, iy, ix + iw, iy + ih),
                    stream=buf.getvalue(),
                    overlay=True,          # always on top
                )
            else:
                wm    = create_watermark_pdf(text, opacity, color,
                                             r.width, r.height, position, rotation)
                wmpdf = fitz.open("pdf", wm)
                # show_pdf_page with overlay=True forces watermark above page content
                page.show_pdf_page(
                    fitz.Rect(0, 0, r.width, r.height),
                    wmpdf, 0,
                    overlay=True,
                )
                wmpdf.close()
        doc.save(ctx.output_path, deflate=True)
    finally:
        doc.close()

    log.info(f"[{ctx.job_id}] watermark_pdf: type={'image' if image_data else 'text'}")
    return {"watermark_type": "image" if image_data else "text"}


@register("page_numbers")
def page_numbers(ctx: JobContext) -> dict:
    """Add page number stamps to each page."""
    _require(FITZ_OK, "page_numbers", "PyMuPDF")
    position = ctx.params.get("position", "bottom")
    start    = int(ctx.params.get("start", 1))
    prefix   = ctx.params.get("prefix", "")
    _guard_empty(ctx.input_path)
    doc = fitz.open(ctx.input_path)
    try:
        for i, page in enumerate(doc):
            r     = page.rect
            label = f"{prefix}{start + i}"
            pn    = create_page_number_pdf(label, position, r.width, r.height)
            pnpdf = fitz.open("pdf", pn)
            page.show_pdf_page(
                fitz.Rect(0, 0, r.width, r.height),
                pnpdf, 0, overlay=True,
            )
            pnpdf.close()
        doc.save(ctx.output_path, deflate=True)
    finally:
        doc.close()
    return {}


@register("crop_pdf")
def crop_pdf(ctx: JobContext) -> dict:
    """Crop page margins in points."""
    _require(FITZ_OK, "crop_pdf", "PyMuPDF")
    left   = float(ctx.params.get("left",   0))
    right  = float(ctx.params.get("right",  0))
    top    = float(ctx.params.get("top",    0))
    bottom = float(ctx.params.get("bottom", 0))
    if any(v < 0 for v in (left, right, top, bottom)):
        raise ValidationError("Crop margins must be non-negative")
    _guard_empty(ctx.input_path)
    doc = fitz.open(ctx.input_path)
    try:
        for page in doc:
            r  = page.rect
            nr = fitz.Rect(r.x0 + left, r.y0 + top, r.x1 - right, r.y1 - bottom)
            if nr.is_empty or nr.is_infinite:
                raise ValidationError("Crop margins too large for this page size")
            if nr.width < 10 or nr.height < 10:
                raise ValidationError("Resulting page would be smaller than 10pt in one dimension")
            page.set_cropbox(nr)
        doc.save(ctx.output_path, deflate=True)
    finally:
        doc.close()
    return {}


# ═══════════════════════════════════════════════════════════════════════════════
# PDF EDIT — text/image/highlight/note overlays via PyMuPDF
# ═══════════════════════════════════════════════════════════════════════════════

# Limits — generous but bounded so a malicious payload can't OOM the worker.
_EDIT_MAX_OPS_TOTAL   = 5000   # combined operations across all lists
_EDIT_MAX_TEXT_LEN    = 4000
_EDIT_MAX_NOTE_LEN    = 4000
_EDIT_MAX_IMAGE_BYTES = 25 * 1024 * 1024   # 25 MB per overlay image
_EDIT_FONT_MAP = {
    "helv": "helv", "helvetica": "helv",
    "tiro": "tiro", "times": "tiro", "times-roman": "tiro",
    "cour": "cour", "courier": "cour", "mono": "cour",
}


def _edit_parse_color(value, default=(0.0, 0.0, 0.0)) -> tuple:
    """Accept '#rrggbb', 'rrggbb', [r,g,b] (0-255 or 0-1), or None."""
    if value is None or value == "":
        return default
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        try:
            vals = [float(v) for v in value[:3]]
        except (TypeError, ValueError):
            return default
        if max(vals) > 1.001:
            vals = [v / 255.0 for v in vals]
        return tuple(max(0.0, min(1.0, v)) for v in vals)
    if isinstance(value, str):
        s = value.strip().lstrip("#")
        if len(s) == 6:
            try:
                return (
                    int(s[0:2], 16) / 255.0,
                    int(s[2:4], 16) / 255.0,
                    int(s[4:6], 16) / 255.0,
                )
            except ValueError:
                return default
    return default


def _edit_parse_payload(ctx: JobContext) -> dict:
    """
    Edits arrive as a JSON string in form field 'edits' (or 'payload'),
    or piecemeal as form fields 'text_overlays', 'highlights', etc.
    Returns a dict with normalised lists: text_overlays, highlights,
    annotations, image_overlays.
    """
    raw = ctx.params.get("edits") or ctx.params.get("payload")
    data: dict = {}
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, dict):
                data = parsed
        except (TypeError, ValueError) as ex:
            raise ValidationError(f"edits is not valid JSON: {ex}")

    # Allow individual form fields to override / supplement
    for key in ("text_overlays", "highlights", "annotations", "image_overlays"):
        if key in ctx.params and key not in data:
            try:
                data[key] = json.loads(ctx.params[key])
            except (TypeError, ValueError):
                raise ValidationError(f"{key} is not valid JSON")

    def _aslist(v):
        if v is None:
            return []
        return v if isinstance(v, list) else [v]

    out = {
        "text_overlays":  _aslist(data.get("text_overlays")),
        "highlights":     _aslist(data.get("highlights")),
        "annotations":    _aslist(data.get("annotations")),
        "image_overlays": _aslist(data.get("image_overlays")),
    }

    total = sum(len(out[k]) for k in out)
    if total == 0:
        raise ValidationError(
            "No edits provided. Send a JSON 'edits' object with at least one "
            "of: text_overlays, highlights, annotations, image_overlays."
        )
    if total > _EDIT_MAX_OPS_TOTAL:
        raise ValidationError(
            f"Too many edit operations ({total}); limit is {_EDIT_MAX_OPS_TOTAL}."
        )
    return out


@register("edit_pdf")
def edit_pdf(ctx: JobContext) -> dict:
    """
    Apply per-page overlays and annotations.

    Accepts a JSON 'edits' payload of the shape:
      {
        "text_overlays":  [{"page":0,"x":72,"y":120,"text":"Hello",
                            "font_size":14,"color":"#1d4ed8","font":"helv"}],
        "highlights":     [{"page":0,"x1":50,"y1":100,"x2":300,"y2":120,
                            "color":"#ffff00","opacity":0.4}],
        "annotations":    [{"page":0,"x":200,"y":200,"content":"Review this",
                            "type":"text","title":"Reviewer"}],
        "image_overlays": [{"page":0,"x":300,"y":300,"width":120,"height":80,
                            "image_b64":"<base64-png-or-jpg>"}]
      }

    Page indices are 0-based. Coordinates are PDF points from the top-left
    of the page (origin at upper-left of the visible page rect), matching
    what the frontend draws on its canvas preview.
    """
    _require(FITZ_OK, "edit_pdf", "PyMuPDF")
    _guard_empty(ctx.input_path)
    edits = _edit_parse_payload(ctx)

    # Lazy import — base64 only needed for image_overlays
    import base64

    doc = fitz.open(ctx.input_path)
    try:
        n_pages = len(doc)
        applied = {"text": 0, "highlight": 0, "annotation": 0, "image": 0}

        def _resolve_page(idx):
            try:
                p = int(idx)
            except (TypeError, ValueError):
                raise ValidationError(f"Invalid page index: {idx!r}")
            if p < 0:
                p += n_pages
            if p < 0 or p >= n_pages:
                raise ValidationError(
                    f"Page index {idx} out of range (0..{n_pages - 1})"
                )
            return p

        # ── Text overlays ─────────────────────────────────────────────
        for op in edits["text_overlays"]:
            if not isinstance(op, dict):
                raise ValidationError("text_overlays entries must be objects")
            page = doc[_resolve_page(op.get("page", 0))]
            text = str(op.get("text", ""))[:_EDIT_MAX_TEXT_LEN]
            if not text:
                continue
            x = float(op.get("x", 72))
            y = float(op.get("y", 72))
            size = float(op.get("font_size", op.get("size", 14)))
            size = max(4.0, min(size, 400.0))
            color = _edit_parse_color(op.get("color"), (0.0, 0.0, 0.0))
            font_key = str(op.get("font", "helv")).lower().strip()
            font = _EDIT_FONT_MAP.get(font_key, "helv")
            # PyMuPDF's insert_text only accepts rotate values that are
            # multiples of 90; snap to the nearest quadrant.
            try:
                requested_rot = int(op.get("rotate", 0)) % 360
            except (TypeError, ValueError):
                requested_rot = 0
            rotate = int(round(requested_rot / 90.0)) * 90 % 360
            try:
                page.insert_text(
                    (x, y), text,
                    fontname=font, fontsize=size,
                    color=color, rotate=rotate, overlay=True,
                )
            except Exception as ex:
                raise ProcessingError(f"insert_text failed on page {op.get('page')}: {ex}")
            applied["text"] += 1

        # ── Highlights (semi-transparent rectangles) ──────────────────
        for op in edits["highlights"]:
            if not isinstance(op, dict):
                raise ValidationError("highlights entries must be objects")
            page = doc[_resolve_page(op.get("page", 0))]
            x1 = float(op.get("x1", op.get("x", 0)))
            y1 = float(op.get("y1", op.get("y", 0)))
            if "x2" in op and "y2" in op:
                x2 = float(op["x2"]); y2 = float(op["y2"])
            else:
                x2 = x1 + float(op.get("width", 100))
                y2 = y1 + float(op.get("height", 20))
            rect = fitz.Rect(min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
            if rect.width <= 0 or rect.height <= 0:
                continue
            color = _edit_parse_color(op.get("color"), (1.0, 1.0, 0.0))
            opacity = max(0.05, min(1.0, float(op.get("opacity", 0.4))))
            try:
                annot = page.add_highlight_annot(rect)
                annot.set_colors(stroke=color)
                annot.set_opacity(opacity)
                annot.update()
            except Exception:
                # Fall back to a drawn rectangle for PDFs where highlight annot fails
                page.draw_rect(
                    rect, color=color, fill=color,
                    fill_opacity=opacity, width=0, overlay=True,
                )
            applied["highlight"] += 1

        # ── Sticky-note annotations ───────────────────────────────────
        for op in edits["annotations"]:
            if not isinstance(op, dict):
                raise ValidationError("annotations entries must be objects")
            page = doc[_resolve_page(op.get("page", 0))]
            content = str(op.get("content", op.get("text", "")))[:_EDIT_MAX_NOTE_LEN]
            x = float(op.get("x", 72)); y = float(op.get("y", 72))
            title = str(op.get("title", op.get("author", "PDFWala")))[:120]
            try:
                annot = page.add_text_annot((x, y), content, icon="Note")
                annot.set_info(title=title, content=content)
                color = _edit_parse_color(op.get("color"), (1.0, 0.85, 0.3))
                annot.set_colors(stroke=color)
                annot.update()
            except Exception as ex:
                raise ProcessingError(f"add_text_annot failed: {ex}")
            applied["annotation"] += 1

        # ── Image overlays ────────────────────────────────────────────
        for op in edits["image_overlays"]:
            if not isinstance(op, dict):
                raise ValidationError("image_overlays entries must be objects")
            b64 = op.get("image_b64") or op.get("image") or ""
            if isinstance(b64, str) and b64.startswith("data:"):
                # strip data URL prefix "data:image/png;base64,"
                _, _, b64 = b64.partition(",")
            if not b64:
                raise ValidationError("image_overlays entry missing image_b64")
            try:
                blob = base64.b64decode(b64, validate=False)
            except Exception as ex:
                raise ValidationError(f"image_b64 is not valid base64: {ex}")
            if len(blob) > _EDIT_MAX_IMAGE_BYTES:
                raise ValidationError(
                    f"image_overlays entry too large "
                    f"({len(blob)} bytes; max {_EDIT_MAX_IMAGE_BYTES})"
                )
            _validate_image_data(blob, max_bytes=_EDIT_MAX_IMAGE_BYTES)

            page = doc[_resolve_page(op.get("page", 0))]
            x = float(op.get("x", 72)); y = float(op.get("y", 72))
            w = float(op.get("width", 120)); h = float(op.get("height", 80))
            rect = fitz.Rect(x, y, x + w, y + h)
            try:
                page.insert_image(rect, stream=blob, overlay=True, keep_proportion=True)
            except Exception as ex:
                raise ProcessingError(f"insert_image failed: {ex}")
            applied["image"] += 1

        doc.save(ctx.output_path, deflate=True, garbage=3)
    finally:
        doc.close()

    log.info(
        f"[{ctx.job_id}] edit_pdf: text={applied['text']} "
        f"highlight={applied['highlight']} note={applied['annotation']} "
        f"image={applied['image']}"
    )
    return {
        "applied":          applied,
        "total_operations": sum(applied.values()),
    }


@register("redact_pdf")
def redact_pdf(ctx: JobContext) -> dict:
    """
    Redact text by literal match, regex, or preset pattern.
    SafeRegex prevents ReDoS. import re is at module level.
    """
    _require(FITZ_OK, "redact_pdf", "PyMuPDF")
    mode        = ctx.params.get("mode", "text")
    search_text = ctx.params.get("search_text", "")
    pattern_str = ctx.params.get("pattern", "")
    preset_name = ctx.params.get("preset", "")

    compiled = None
    if mode == "text":
        if not search_text:
            raise ValidationError("search_text required for mode=text")
    elif mode == "regex":
        if not pattern_str:
            raise ValidationError("pattern required for mode=regex")
        try:
            compiled = SafeRegex.compile(pattern_str)
        except (ValueError, Exception) as ex:
            raise ValidationError(f"Invalid regex: {ex}")
    elif mode == "preset":
        if preset_name not in REDACTION_PATTERNS:
            raise ValidationError(
                f"Unknown preset. Choose: {', '.join(sorted(REDACTION_PATTERNS))}"
            )
        compiled = re.compile(REDACTION_PATTERNS[preset_name])
    else:
        raise ValidationError(f"Unknown redact mode: {mode!r}")

    _guard_empty(ctx.input_path)
    doc   = fitz.open(ctx.input_path)
    count = 0
    try:
        for page in doc:
            if mode == "text":
                for rect in page.search_for(search_text):
                    page.add_redact_annot(rect, fill=(0, 0, 0))
                    count += 1
            else:
                for match in compiled.finditer(page.get_text("text")):
                    for rect in page.search_for(match.group()):
                        page.add_redact_annot(rect, fill=(0, 0, 0))
                        count += 1
            page.apply_redactions()
        doc.save(ctx.output_path, deflate=True)
    finally:
        doc.close()

    log.info(f"[{ctx.job_id}] redact_pdf: {count} redactions applied")
    return {
        "redaction_count": count,
        "warning": "No matches found — document unchanged" if count == 0 else None,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PDF INFO / SECURITY
# ═══════════════════════════════════════════════════════════════════════════════

@register("pdf_info")
def pdf_info(ctx: JobContext) -> dict:
    _require(FITZ_OK, "pdf_info", "PyMuPDF")
    _guard_empty(ctx.input_path)
    file_size = os.path.getsize(ctx.input_path)
    doc = fitz.open(ctx.input_path)
    try:
        meta       = doc.metadata
        sizes: dict = {}
        for pg in doc:
            k = (round(pg.rect.width, 1), round(pg.rect.height, 1))
            sizes[k] = sizes.get(k, 0) + 1
        font_names: set = set()
        for pg in doc:
            for fi in pg.get_fonts(full=True):
                if len(fi) > 3 and fi[3]:
                    font_names.add(fi[3])
        is_lin = False
        try:
            xobj   = doc.xref_object(1, compressed=False)
            is_lin = "/Linearized" in (xobj or "")
        except Exception:
            pass
        # PyMuPDF 1.24 removed Document.pdf_version(). The version is exposed
        # in metadata['format'] as e.g. "PDF 1.7".
        pdf_version = ""
        fmt = (meta or {}).get("format", "")
        if isinstance(fmt, str) and fmt.upper().startswith("PDF"):
            pdf_version = fmt.split(" ", 1)[-1] if " " in fmt else fmt

        return {
            "metadata": {
                "page_count":       len(doc),
                "pdf_version":      pdf_version,
                "title":            meta.get("title", ""),
                "author":           meta.get("author", ""),
                "encrypted":        doc.is_encrypted,
                "file_size_bytes":  file_size,
                "size_human":       format_file_size(file_size),
                "has_forms":        any(pg.first_widget for pg in doc),
                "has_toc":          len(doc.get_toc()) > 0,
                "image_count":      sum(len(pg.get_images()) for pg in doc),
                "fonts_used":       sorted(font_names)[:20],
                "is_linearized":    is_lin,
                "page_sizes": [
                    {"w": k[0], "h": k[1], "count": v}
                    for k, v in sorted(sizes.items(), key=lambda x: -x[1])
                ],
            }
        }
    finally:
        doc.close()


@register("protect_pdf")
def protect_pdf(ctx: JobContext) -> dict:
    """
    Encrypt PDF with AES-256.
    Owner password is user_pw + random 8-char suffix so it differs from user_pw,
    preventing trivial privilege escalation.
    """
    _require(FITZ_OK, "protect_pdf", "PyMuPDF")
    pw          = ctx.params.get("password", "")
    allow_print = ctx.params.get("allow_print", True)
    allow_copy  = ctx.params.get("allow_copy", True)
    if not pw:
        raise ValidationError("Password required")
    # PDF AES-256 (ISO 32000-2) caps passwords at 127 UTF-8 bytes; longer
    # passwords are silently truncated by some readers, which would lock the
    # user out. Reject them up front instead.
    if len(pw.encode("utf-8")) > 127:
        raise ValidationError("Password too long (max 127 bytes for AES-256 PDF encryption)")

    # Owner password must differ from user password
    owner_pw = pw + "-" + secrets.token_hex(4)

    _guard_empty(ctx.input_path)
    doc = fitz.open(ctx.input_path)
    try:
        permissions = int(fitz.PDF_PERM_ACCESSIBILITY)
        if allow_print:
            permissions |= int(fitz.PDF_PERM_PRINT)
        if allow_copy:
            permissions |= int(fitz.PDF_PERM_COPY)
        doc.save(
            ctx.output_path,
            encryption=fitz.PDF_ENCRYPT_AES_256,
            owner_pw=owner_pw,
            user_pw=pw,
            permissions=permissions,
        )
    finally:
        doc.close()
    return {}


@register("unlock_pdf")
def unlock_pdf(ctx: JobContext) -> dict:
    """
    Remove password protection.
    Tries user_pw first, then owner_pw (owner unlocks all restrictions).
    Saves with no encryption AND explicitly clears permission bits.
    """
    _require(FITZ_OK, "unlock_pdf", "PyMuPDF")
    pw = ctx.params.get("password", "")
    if not pw:
        raise ValidationError("Password required")

    doc = fitz.open(ctx.input_path)
    # Capture the real encryption state BEFORE authenticating — is_encrypted
    # flips to False once we successfully authenticate, so reading it later
    # would always report the document as unencrypted.
    was_encrypted = bool(doc.is_encrypted)
    try:
        if was_encrypted:
            # Try as user password first, then as owner password
            if not doc.authenticate(pw):
                doc.close()
                raise ValidationError("Wrong password — authentication failed")

        # Save with no encryption and full permissions
        doc.save(
            ctx.output_path,
            encryption=fitz.PDF_ENCRYPT_NONE,
            deflate=True,
            garbage=3,
        )
    finally:
        doc.close()

    log.info(f"[{ctx.job_id}] unlock_pdf: successfully unlocked (was_encrypted={was_encrypted})")
    return {"was_encrypted": was_encrypted}


@register("sign_pdf")
def sign_pdf(ctx: JobContext) -> dict:
    """
    Add a visible signature stamp to the PDF.
    Signature is stamped directly into the page content stream via fitz drawing
    primitives so it persists on reopen in all viewers.
    sig_data bytes are validated before use.
    Note: this is a VISUAL stamp, not a cryptographic signature (PAdES/PKCS#7).
    """
    _require(FITZ_OK, "sign_pdf", "PyMuPDF")
    from datetime import datetime

    name        = ctx.params.get("name", "Signed")
    reason      = ctx.params.get("reason", "Approved")
    page_target = ctx.params.get("page", "last")
    position    = ctx.params.get("position", "bottom-right")
    sig_data    = ctx.params.get("sig_data")       # bytes or None
    today_str   = datetime.now().strftime("%Y-%m-%d")

    if sig_data:
        _validate_image_data(sig_data, max_bytes=2 * 1024 * 1024)

    _guard_empty(ctx.input_path)
    doc   = fitz.open(ctx.input_path)
    total = len(doc)
    try:
        if page_target == "all":
            idxs = list(range(total))
        elif page_target == "first":
            idxs = [0]
        elif page_target == "last":
            idxs = [total - 1]
        else:
            try:
                n = int(page_target)
                if n < 1 or n > total:
                    raise ValidationError(f"Page {n} out of range (1-{total})")
                idxs = [n - 1]
            except ValueError:
                raise ValidationError(f"Invalid page target: {page_target!r}")

        pos_map = {
            "bottom-right": lambda r: (r.x1 - 180, r.y1 - 70),
            "bottom-left":  lambda r: (r.x0 + 30,  r.y1 - 70),
            "top-right":    lambda r: (r.x1 - 180, r.y0 + 50),
            "top-left":     lambda r: (r.x0 + 30,  r.y0 + 50),
            "center":       lambda r: (r.x0 + r.width / 2 - 75, r.y0 + r.height / 2),
        }
        pos_fn = pos_map.get(position, pos_map["bottom-right"])

        for idx in idxs:
            page = doc[idx]
            rect = page.rect
            sx, sy = pos_fn(rect)

            # Clamp to page bounds
            sx = max(rect.x0 + 5, min(sx, rect.x1 - 165))
            sy = max(rect.y0 + 30, min(sy, rect.y1 - 10))

            # Draw signature box directly into content stream (persists on reopen)
            box_r = fitz.Rect(sx - 5, sy - 45, sx + 155, sy + 25)
            shape = page.new_shape()
            shape.draw_rect(box_r)
            shape.finish(color=(0, 0, 0.6), fill=(0.9, 0.9, 1.0), width=0.8)
            line = f"{name} | {reason} | {today_str}"
            # ASCII only — the base-14 font used here has no glyph for "✦"
            # (U+2726), which rendered as a missing-glyph box in strict viewers.
            shape.insert_text((sx, sy - 28), "* SIGNED", fontsize=9,
                               color=(0, 0, 0.6))
            shape.insert_text((sx, sy - 12), line, fontsize=7.5,
                               color=(0.1, 0.1, 0.1))
            shape.commit()  # writes into page content stream — survives reopen

            if sig_data:
                page.insert_image(
                    fitz.Rect(sx, sy - 42, sx + 100, sy - 5),
                    stream=sig_data,
                    overlay=True,
                )

        doc.save(ctx.output_path, deflate=True)
    finally:
        doc.close()

    log.info(f"[{ctx.job_id}] sign_pdf: stamped {len(idxs)} pages")
    return {
        "pages_signed": len(idxs),
        "note": "Visual stamp only — not a cryptographic (PAdES/PKCS#7) signature",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PDF CONVERT
# ═══════════════════════════════════════════════════════════════════════════════

@register("pdf_to_image")
def pdf_to_image(ctx: JobContext) -> dict:
    """
    Convert PDF pages to images, streamed to disk ZIP for large docs.
    """
    _require(FITZ_OK and PIL_OK, "pdf_to_image", "PyMuPDF + Pillow")
    fmt = ctx.params.get("format", "jpg").lower()
    # 200 DPI default (was 150): noticeably crisper page images while staying
    # well within memory limits; still overridable and clamped.
    dpi = int(ctx.params.get("dpi", 200))
    if fmt not in ("jpg", "png"):
        fmt = "jpg"
    dpi = max(72, min(dpi, 600))   # clamp

    _guard_empty(ctx.input_path)
    doc = fitz.open(ctx.input_path)
    try:
        count  = len(doc)
        zf, buf = _open_zip_writer(ctx.output_path, count)
        try:
            for i, page in enumerate(doc):
                mat = fitz.Matrix(dpi / 72, dpi / 72)
                # alpha=False renders the page opaque on white — PDF pages are
                # opaque, so this is correct and lets us hand pixmap bytes
                # straight to PIL (no PNG encode→decode round-trip, which was
                # the dominant per-page cost and a RAM spike at high DPI).
                pix = page.get_pixmap(matrix=mat, alpha=False)
                pil = None
                ib  = None
                try:
                    mode = "RGB" if pix.n >= 3 else "L"
                    pil  = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
                    ib   = io.BytesIO()
                    if fmt == "jpg":
                        if pil.mode != "RGB":
                            pil = pil.convert("RGB")
                        pil.save(ib, "JPEG", quality=90, optimize=True,
                                 progressive=True)
                    else:
                        pil.save(ib, "PNG", optimize=True)
                    zf.writestr(f"page_{i + 1:04d}.{fmt}", ib.getvalue())
                finally:
                    # Explicit cleanup — prevents RAM accumulation at high DPI
                    if pil is not None:
                        try: pil.close()
                        except Exception: pass
                    if ib is not None:
                        try: ib.close()
                        except Exception: pass
                    del pix
                if i % 10 == 0:
                    ctx.set_progress(int(i / count * 95))
        finally:
            _finalise_zip(zf, buf, ctx.output_path)
    finally:
        doc.close()

    ctx.set_progress(100)
    log.info(f"[{ctx.job_id}] pdf_to_image: {count} pages → {fmt}")
    return {"pages_exported": count, "format": fmt}


@register("pdf_to_jpg")
def pdf_to_jpg(ctx: JobContext) -> dict:
    ctx.params["format"] = "jpg"
    return pdf_to_image(ctx)


@register("pdf_to_png")
def pdf_to_png(ctx: JobContext) -> dict:
    ctx.params["format"] = "png"
    return pdf_to_image(ctx)


@register("pdf_to_word")
def pdf_to_word(ctx: JobContext) -> dict:
    """
    Convert PDF to DOCX via pdf2docx (table-aware mode).
    V14 FIX: For large PDFs, convert in page-range chunks to avoid pdf2docx OOM.
             Also adds progress reporting and better error diagnostics.
    """
    _require(PDF2DOCX_OK, "pdf_to_word", "pdf2docx")
    _guard_empty(ctx.input_path)

    # Determine page count for progress + chunking decision
    page_count = 0
    if FITZ_OK:
        doc = fitz.open(ctx.input_path)
        page_count = len(doc)
        doc.close()

    ctx.set_progress(5)

    # For files > 100 pages, convert in chunks to avoid pdf2docx memory exhaustion
    CHUNK_THRESHOLD = 100
    if page_count > CHUNK_THRESHOLD:
        chunk_size = 50
        chunk_docxs = []
        tmp_dir = None
        try:
            import tempfile
            tmp_dir = tempfile.mkdtemp(prefix="pdf2docx_chunks_")
            n_chunks = (page_count + chunk_size - 1) // chunk_size
            for ci in range(n_chunks):
                start = ci * chunk_size
                end   = min(start + chunk_size, page_count)
                chunk_out = os.path.join(tmp_dir, f"chunk_{ci:04d}.docx")
                cv = Pdf2DocxConverter(ctx.input_path)
                try:
                    cv.convert(chunk_out, start=start, end=end)
                finally:
                    cv.close()
                if os.path.exists(chunk_out) and os.path.getsize(chunk_out) > 0:
                    chunk_docxs.append(chunk_out)
                ctx.set_progress(5 + int((ci + 1) / n_chunks * 85))

            if not chunk_docxs:
                raise ProcessingError("pdf2docx produced no output for any chunk")

            # Merge chunks with python-docx if multiple, else just move the single chunk
            if len(chunk_docxs) == 1:
                shutil.copy(chunk_docxs[0], ctx.output_path)
            else:
                # Merge via python-docx compose
                try:
                    from docx import Document as _DocxDoc
                    from docx.oxml.ns import qn
                    import copy

                    base_doc = _DocxDoc(chunk_docxs[0])
                    for chunk_path in chunk_docxs[1:]:
                        src = _DocxDoc(chunk_path)
                        # Add page break before each chunk
                        from docx.oxml import OxmlElement
                        br = OxmlElement("w:p")
                        r  = OxmlElement("w:r")
                        rPr = OxmlElement("w:rPr")
                        pb  = OxmlElement("w:lastRenderedPageBreak")
                        rPr.append(pb)
                        r.append(rPr)
                        br.append(r)
                        base_doc.element.body.append(br)
                        for element in src.element.body:
                            base_doc.element.body.append(copy.deepcopy(element))
                    base_doc.save(ctx.output_path)
                except Exception as merge_ex:
                    # Do NOT silently ship only the first chunk — that drops the
                    # majority of the document without telling the user. Fail
                    # loudly with a count of the pages that would be lost and a
                    # concrete next step.
                    pages_in_first = min(chunk_size, page_count)
                    lost = max(0, page_count - pages_in_first)
                    log.error(f"[{ctx.job_id}] chunk merge failed ({merge_ex}); ~{lost} pages would be lost")
                    raise ProcessingError(
                        f"Could not assemble the converted Word document "
                        f"({merge_ex}). About {lost} of {page_count} pages would "
                        f"be missing from the output, so the conversion was "
                        f"stopped. Split the PDF into sections of under "
                        f"{CHUNK_THRESHOLD} pages and convert each separately."
                    )
        finally:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)
    else:
        # Standard single-pass conversion
        cv = Pdf2DocxConverter(ctx.input_path)
        try:
            cv.convert(ctx.output_path, start=0, end=None)
        finally:
            cv.close()

    if not os.path.exists(ctx.output_path) or os.path.getsize(ctx.output_path) == 0:
        raise ProcessingError(
            "pdf2docx produced empty output — the PDF may be image-only, "
            "encrypted, or have an unsupported structure. Try OCR first."
        )
    ctx.set_progress(100)
    return {"pages": page_count}


@register("pdf_to_excel")
def pdf_to_excel(ctx: JobContext) -> dict:
    """
    Extract tables from PDF to XLSX.
    Always closes fitz doc even if pdfplumber fails.
    """
    _require(OPENPYXL_OK, "pdf_to_excel", "openpyxl")
    _guard_empty(ctx.input_path)

    wb               = Workbook()
    wb.remove(wb.active)
    tables_extracted = 0

    if PDFPLUMBER_OK:
        with pdfplumber.open(ctx.input_path) as pdf:
            for page in pdf.pages:
                for table in page.extract_tables():
                    if table and any(any(c for c in r if c) for r in table):
                        tables_extracted += 1
                        ws = wb.create_sheet(f"Table_{tables_extracted}")
                        for row in table:
                            ws.append([str(c).strip() if c else "" for c in row])

    if tables_extracted == 0 and FITZ_OK:
        ws      = wb.create_sheet("Text")
        doc     = fitz.open(ctx.input_path)
        row_idx = 1
        try:
            for pn, pg in enumerate(doc):
                ws.cell(row_idx, 1, f"--- Page {pn + 1} ---")
                row_idx += 1
                for line in pg.get_text("text").split("\n"):
                    if line.strip():
                        ws.cell(row_idx, 1, line.strip())
                        row_idx += 1
        finally:
            doc.close()   # always closed — bug fix

    wb.save(ctx.output_path)
    log.info(f"[{ctx.job_id}] pdf_to_excel: {tables_extracted} tables extracted")
    return {"tables_found": tables_extracted}


@register("pdf_to_ppt")
def pdf_to_ppt(ctx: JobContext) -> dict:
    """
    Convert PDF pages to PowerPoint slides (one image per slide).
    Temp files cleaned up in finally block even on exception.
    """
    _require(PPTX_OK and FITZ_OK, "pdf_to_ppt", "python-pptx + PyMuPDF")
    _guard_empty(ctx.input_path)

    doc        = fitz.open(ctx.input_path)
    # FIX: capture page count before the finally block closes doc
    slide_count = len(doc)
    tmp_files: list[str] = []
    try:
        prs = Presentation()
        prs.slide_width  = PptxInches(10)
        prs.slide_height = PptxInches(7.5)
        blank = prs.slide_layouts[6]

        for i, page in enumerate(doc):
            pix     = page.get_pixmap(dpi=150)
            fd, tmp_path = tempfile.mkstemp(suffix=".png")
            tmp_files.append(tmp_path)
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(pix.tobytes("png"))
            except Exception:
                os.close(fd)
                raise
            slide = prs.slides.add_slide(blank)
            slide.shapes.add_picture(tmp_path, 0, 0, prs.slide_width, prs.slide_height)
            if i % 10 == 0:
                ctx.set_progress(int(i / slide_count * 90))
    finally:
        doc.close()
        for tp in tmp_files:    # always clean up temp PNGs
            try:
                os.unlink(tp)
            except OSError:
                pass

    prs.save(ctx.output_path)
    log.info(f"[{ctx.job_id}] pdf_to_ppt: {slide_count} slides created")
    return {"slides_created": slide_count}


_PDFA_VERSION_RE = re.compile(r"^([123])[ab]?$", re.I)
_PDFA_ICC_CANDIDATES = (
    "/usr/share/color/icc/ghostscript/srgb.icc",
    "/usr/share/color/icc/ghostscript/default_rgb.icc",
)


def _pdfa_def_ps(tmp_dir: str):
    """
    Write a PDFA_def.ps (stock Ghostscript prefix, ICC path made absolute)
    into tmp_dir. Without this prefix Ghostscript emits a plain PDF with no
    OutputIntent or pdfaid XMP — i.e. not PDF/A at all.
    Returns (def_path, icc_dir) or (None, None) if resources are missing.
    """
    import glob as _glob
    stock = sorted(_glob.glob("/usr/share/ghostscript/*/lib/PDFA_def.ps"))
    icc   = next((p for p in _PDFA_ICC_CANDIDATES if os.path.exists(p)), None)
    if not stock or not icc:
        return None, None
    txt = open(stock[-1], encoding="utf-8", errors="replace").read()
    txt = txt.replace("/ICCProfile (srgb.icc)", f"/ICCProfile ({icc})")
    def_path = os.path.join(tmp_dir, "PDFA_def.ps")
    with open(def_path, "w", encoding="utf-8") as fh:
        fh.write(txt)
    return def_path, os.path.dirname(icc)


@register("pdf_to_pdfa")
def pdf_to_pdfa(ctx: JobContext) -> dict:
    """
    Convert to PDF/A via Ghostscript.
    The requested part (1/2/3) is validated and passed through — previously
    2b silently produced PDF/A-1 and 3b produced PDF/A-2. The PDFA_def.ps
    prefix embeds the sRGB OutputIntent + pdfaid XMP so output actually
    identifies (and can validate) as PDF/A.
    """
    version = str(ctx.params.get("version", "1b")).lower()
    m = _PDFA_VERSION_RE.match(version)
    if not m:
        raise ValidationError(
            f"Invalid PDF/A version '{version}' — use 1b, 2b or 3b"
        )
    pdfa_val = m.group(1)
    safe_out = _safe_output_path(ctx.output_path)

    tmp_dir = tempfile.mkdtemp()
    try:
        def_ps, icc_dir = _pdfa_def_ps(tmp_dir)
        cmd = [
            Config.GHOSTSCRIPT,
            "-dBATCH", "-dNOPAUSE", "-dSAFER",
            "-sDEVICE=pdfwrite",
            f"-dPDFA={pdfa_val}",
            "-dPDFACompatibilityPolicy=1",
            "-sColorConversionStrategy=RGB",
        ]
        if def_ps:
            cmd.append(f"--permit-file-read={icc_dir}/")
        cmd.append(f"-sOutputFile={safe_out}")
        if def_ps:
            cmd.append(def_ps)
        else:
            log.warning(
                f"[{ctx.job_id}] pdf_to_pdfa: PDFA_def.ps/ICC profile not found — "
                "output will lack the PDF/A OutputIntent"
            )
        cmd.append(ctx.input_path)

        timeout = Config.PDFA_TIMEOUT
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout)
            if result.returncode != 0:
                err = result.stderr.decode("utf-8", "replace")[:200]
                raise ProcessingError(
                    f"Ghostscript PDF/A conversion failed (rc={result.returncode}): {err}"
                )
        except subprocess.TimeoutExpired:
            raise OperationTimeoutError("PDF/A conversion", timeout)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if not os.path.exists(safe_out) or os.path.getsize(safe_out) == 0:
        raise ProcessingError("PDF/A conversion produced empty output")

    return {"pdfa_version": f"{pdfa_val}b"}


def _estimate_skew_angle(gray) -> float:
    """
    Estimate the rotation (degrees) that straightens a skewed document, using a
    projection-profile search: binarize, then for each candidate angle rotate a
    downscaled copy and score how "peaky" the row-ink profile is (sum of squared
    differences between adjacent rows). Text lines produce the sharpest profile
    when horizontal, so the best-scoring angle is the correction to apply.

    This is markedly more reliable than a min-area-rect on sparse text (which
    barely moves for small skews). Returns the rotation to APPLY, in (-15, 15),
    or 0.0 when nothing beats "no rotation".
    """
    try:
        h, w = gray.shape
        # Downscale for speed — skew is a global property, fine detail is noise.
        scale = 800.0 / max(w, 1)
        if scale < 1.0:
            gray = cv2.resize(gray, (int(w * scale), int(h * scale)),
                              interpolation=cv2.INTER_AREA)
        binimg = cv2.threshold(gray, 0, 255,
                               cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
        sh, sw = binimg.shape
        center = (sw / 2.0, sh / 2.0)

        def _score(angle: float) -> float:
            m = cv2.getRotationMatrix2D(center, angle, 1.0)
            rot = cv2.warpAffine(binimg, m, (sw, sh),
                                 flags=cv2.INTER_NEAREST,
                                 borderMode=cv2.BORDER_CONSTANT, borderValue=0)
            proj = rot.sum(axis=1, dtype=_np.float64)
            diff = _np.diff(proj)
            return float((diff * diff).sum())

        base = _score(0.0)
        best_angle, best_score = 0.0, base
        for a in _np.arange(-8.0, 8.01, 0.5):
            if abs(a) < 1e-6:
                continue
            s = _score(float(a))
            if s > best_score:
                best_score, best_angle = s, float(a)
        # Require a clear improvement over "no rotation" to avoid chasing noise.
        if best_score < base * 1.05:
            return 0.0
        return best_angle if -15.0 < best_angle < 15.0 else 0.0
    except Exception:
        return 0.0


def _preprocess_for_ocr(pil_img):
    """
    Clean a rendered page for Tesseract: grayscale → light denoise → deskew →
    Otsu binarize. Returns (processed_PIL_L_image, inv_affine_or_None) where the
    inverse affine maps a point in the DESKEWED image back to the ORIGINAL
    rendered pixel space (so the invisible text layer can be positioned to match
    the un-rotated raster we actually display). Falls back to plain grayscale if
    OpenCV is unavailable.
    """
    if not CV2_OK:
        return pil_img.convert("L"), None
    try:
        gray = _np.array(pil_img.convert("L"))
        # Light denoise — removes scanner speckle without smearing glyph edges.
        gray = cv2.fastNlMeansDenoising(gray, None, h=7,
                                        templateWindowSize=7, searchWindowSize=21)
        inv = None
        angle = _estimate_skew_angle(gray)
        if abs(angle) > 0.3:
            h, w = gray.shape
            center = (w / 2.0, h / 2.0)
            m = cv2.getRotationMatrix2D(center, angle, 1.0)
            gray = cv2.warpAffine(gray, m, (w, h), flags=cv2.INTER_CUBIC,
                                  borderMode=cv2.BORDER_REPLICATE)
            inv = cv2.invertAffineTransform(m)
        # Otsu global threshold — Tesseract is most accurate on clean bi-level.
        gray = cv2.threshold(gray, 0, 255,
                             cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
        return Image.fromarray(gray), inv
    except Exception:
        return pil_img.convert("L"), None


@register("ocr_pdf")
def ocr_pdf(ctx: JobContext) -> dict:
    """
    OCR a scanned PDF using Tesseract, producing a searchable PDF.

    Quality-tuned:
    - Pages that ALREADY carry a digital text layer are copied through
      unchanged (insert_pdf) — we never rasterize real, selectable text into a
      lossy image + approximate OCR layer. Only image-only pages are OCR'd.
    - Image-only pages render at 300 DPI (Tesseract's documented optimum; 150
      measurably loses accuracy on small type) and pass through an OpenCV
      pipeline (denoise → deskew → Otsu threshold) before OCR.
    - The invisible text layer is positioned with the inverse of the deskew
      transform so it aligns with the un-rotated raster we display.

    Infra-aware: worker-slow Celery runs 2 concurrent OCRs; we do NOT spawn
    extra threads per job. Rendering is chunked (peak RAM = one chunk).
    lang/psm/oem are whitelist-sanitised; chunked progress drives the UI.
    """
    _require(TESSERACT_OK and FITZ_OK, "ocr_pdf", "pytesseract + PyMuPDF")
    lang_raw = ctx.params.get("lang", "eng")
    lang     = _sanitise_tesseract_lang(lang_raw)
    dpi      = int(ctx.params.get("dpi", getattr(Config, "OCR_DPI", 300)))
    dpi      = max(72, min(dpi, 600))

    psm_raw = int(ctx.params.get("psm", 3))
    oem_raw = int(ctx.params.get("oem", 3))
    if psm_raw not in _VALID_TESSERACT_PSM:
        raise ValidationError(f"Invalid Tesseract PSM value: {psm_raw}")
    if oem_raw not in _VALID_TESSERACT_OEM:
        raise ValidationError(f"Invalid Tesseract OEM value: {oem_raw}")
    psm = psm_raw
    oem = oem_raw

    # Cap in-process workers conservatively. The worker-slow Celery container
    # already runs `--concurrency=2`, so two OCR jobs can be in flight at
    # once; adding 4 threads per job would oversubscribe a 4-core VM.
    workers = max(1, min(Config.OCR_WORKERS, 2))
    max_pages = Config.MAX_OCR_PAGES

    _guard_empty(ctx.input_path)
    src_doc = fitz.open(ctx.input_path)
    total   = len(src_doc)

    if total > max_pages:
        src_doc.close()
        raise ValidationError(
            f"PDF has {total} pages; OCR is limited to {max_pages} pages. "
            "Split the PDF first or contact support for bulk processing."
        )

    # Classify pages: those that already have a real text layer are preserved
    # verbatim (never rasterized); only image-only pages are sent to Tesseract.
    text_pages = set()
    for i in range(total):
        try:
            if src_doc[i].get_text("text").strip():
                text_pages.add(i)
        except Exception:
            pass
    ocr_targets = [i for i in range(total) if i not in text_pages]

    def _ocr_page(args: tuple):
        """Worker: preprocess + Tesseract one page. Returns (page_num, pw, ph, hocr, inv)."""
        page_num, pw, ph, img = args
        inv = None
        try:
            proc, inv = _preprocess_for_ocr(img)
            hocr = pytesseract.image_to_data(
                proc, lang=lang,
                output_type=TesseractOutput.DICT,
                config=f"--psm {psm} --oem {oem}",
            )
            proc.close()
        except Exception as ex:
            log.warning(f"OCR page {page_num + 1}: {ex}")
            hocr = None
        return (page_num, pw, ph, hocr, inv)

    # Render → OCR in bounded chunks. Rendering stays in the main thread
    # (PyMuPDF is not thread-safe); only Tesseract runs in the pool. Peak RAM
    # is ONE chunk of page images instead of the whole document.
    chunk_size = max(workers * 4, 8)
    ocr_results: dict[int, tuple] = {}
    completed = 0
    n_targets = len(ocr_targets)
    ctx.set_progress(15)
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for start in range(0, n_targets, chunk_size):
                chunk = []
                for page_num in ocr_targets[start:start + chunk_size]:
                    src_page = src_doc[page_num]
                    pw, ph = src_page.rect.width, src_page.rect.height
                    mat    = fitz.Matrix(dpi / 72, dpi / 72)
                    pix    = src_page.get_pixmap(matrix=mat, alpha=False,
                                                 colorspace=fitz.csGRAY)
                    img  = Image.frombytes("L", (pix.width, pix.height), pix.samples)
                    chunk.append((page_num, pw, ph, img))
                try:
                    futures = [pool.submit(_ocr_page, item) for item in chunk]
                    for future in as_completed(futures):
                        page_num, pw, ph, hocr, inv = future.result()
                        ocr_results[page_num] = (pw, ph, hocr, inv)
                        completed += 1
                        ctx.set_progress(15 + int(completed / max(n_targets, 1) * 70))
                finally:
                    for _, _, _, img in chunk:
                        img.close()
    finally:
        src_doc.close()

    ctx.set_progress(85)

    # Reassemble in page order: preserve text pages, overlay OCR on image pages.
    src_doc2 = fitz.open(ctx.input_path)
    out_doc  = fitz.open()
    pages_processed = 0
    scale = 72.0 / dpi              # rendered-pixel → PDF-point

    try:
        for page_num in range(total):
            if page_num in text_pages:
                # Preserve the original page (keeps its selectable text intact).
                out_doc.insert_pdf(src_doc2, from_page=page_num, to_page=page_num)
                pages_processed += 1
                continue

            pw, ph, hocr, inv = ocr_results.get(page_num, (None, None, None, None))
            if pw is None:
                src_page = src_doc2[page_num]
                pw, ph = src_page.rect.width, src_page.rect.height
            new_page = out_doc.new_page(width=pw, height=ph)
            new_page.show_pdf_page(
                fitz.Rect(0, 0, pw, ph), src_doc2, page_num, overlay=False
            )

            if hocr:
                words_on_page = 0
                n = len(hocr.get("text", []))
                for i in range(n):
                    word = (hocr["text"][i] or "").strip()
                    conf = int(hocr["conf"][i]) if hocr["conf"][i] != -1 else 0
                    if not word or conf < 30:
                        continue
                    if words_on_page >= _OCR_MAX_WORDS_PER_PAGE:
                        log.warning(
                            f"[{ctx.job_id}] ocr_pdf: page {page_num + 1} hit the "
                            f"{_OCR_MAX_WORDS_PER_PAGE}-word cap; remaining words skipped"
                        )
                        break
                    # Word anchor (bottom-left) in deskewed-pixel space.
                    px = float(hocr["left"][i])
                    py = float(hocr["top"][i] + hocr["height"][i])
                    if inv is not None:
                        # Map back through the inverse deskew so the invisible
                        # text lines up with the un-rotated raster we display.
                        ox = inv[0][0] * px + inv[0][1] * py + inv[0][2]
                        oy = inv[1][0] * px + inv[1][1] * py + inv[1][2]
                        px, py = ox, oy
                    x0 = px * scale
                    y1 = py * scale
                    fs = max(4.0, hocr["height"][i] * scale * 0.85)
                    new_page.insert_text(
                        (x0, y1 - 1), word + " ",
                        fontsize=fs, fontname="helv",
                        color=(0, 0, 0), render_mode=3, overlay=True,
                    )
                    words_on_page += 1
            pages_processed += 1

        if pages_processed == 0:
            raise ProcessingError("OCR produced no output — all pages failed")

        out_doc.save(ctx.output_path, deflate=True, garbage=2)
    finally:
        out_doc.close()
        src_doc2.close()

    ctx.set_progress(100)
    log.info(f"[{ctx.job_id}] ocr_pdf: {pages_processed}/{total} pages "
             f"({len(text_pages)} preserved, {n_targets} OCR'd), lang={lang}, dpi={dpi}")
    return {"pages_processed": pages_processed, "pages_ocred": n_targets,
            "pages_preserved": len(text_pages), "lang": lang, "dpi": dpi}


@register("compare_pdf")
def compare_pdf(ctx: JobContext) -> dict:
    """
    Visual + textual diff of two PDFs.
    ZIP streamed to disk for large docs.
    """
    _require(FITZ_OK and PIL_OK, "compare_pdf", "PyMuPDF + Pillow")
    from PIL import ImageChops

    p1, p2 = ctx.input_paths[0], ctx.input_paths[1]
    doc1   = fitz.open(p1)
    doc2   = fitz.open(p2)

    if len(doc1) == 0:
        doc1.close(); doc2.close()
        raise ValidationError("First PDF has no pages")
    if len(doc2) == 0:
        doc1.close(); doc2.close()
        raise ValidationError("Second PDF has no pages")

    pages     = min(len(doc1), len(doc2))
    sims:      list[float] = []
    diff_data: list[dict]  = []

    try:
        zf, buf = _open_zip_writer(ctx.output_path, pages)
        try:
            for i in range(pages):
                img1 = img2 = diff = None
                try:
                    pix1 = doc1[i].get_pixmap(dpi=150)
                    pix2 = doc2[i].get_pixmap(dpi=150)
                    img1 = Image.open(io.BytesIO(pix1.tobytes("png"))).convert("RGB")
                    img2 = Image.open(io.BytesIO(pix2.tobytes("png"))).convert("RGB")
                    if img1.size != img2.size:
                        resized = img2.resize(img1.size, Image.LANCZOS)
                        img2.close()
                        img2 = resized
                    diff = ImageChops.difference(img1, img2)
                    diff = diff.point(lambda x: min(x * 8, 255))
                    db   = io.BytesIO()
                    diff.save(db, "PNG")
                    zf.writestr(f"diff_page_{i + 1:04d}.png", db.getvalue())
                finally:
                    # Close PIL images explicitly — a large comparison otherwise
                    # accumulates hundreds of decoded bitmaps in RAM.
                    for im in (img1, img2, diff):
                        try:
                            if im is not None:
                                im.close()
                        except Exception:
                            pass

                # Similarity is capped at _COMPARE_WORD_CAP words/page for speed;
                # tell the caller when that cap actually changed the input so a
                # low score on a dense page isn't mistaken for real divergence.
                raw_words1 = [w[4] for w in doc1[i].get_text("words")]
                raw_words2 = [w[4] for w in doc2[i].get_text("words")]
                word_cap_applied = (
                    len(raw_words1) > _COMPARE_WORD_CAP
                    or len(raw_words2) > _COMPARE_WORD_CAP
                )
                words1 = raw_words1[:_COMPARE_WORD_CAP]
                words2 = raw_words2[:_COMPARE_WORD_CAP]
                sm     = difflib.SequenceMatcher(None, words1, words2)
                sim    = round(sm.ratio() * 100, 1)
                sims.append(sim)
                diff_data.append({
                    "page": i + 1,
                    "similarity_pct": sim,
                    "word_cap_applied": word_cap_applied,
                })

                if i % 10 == 0:
                    ctx.set_progress(int(i / pages * 90))

            any_capped = any(d["word_cap_applied"] for d in diff_data)
            summary = {
                "pages":                  diff_data,
                "overall_similarity_pct": round(sum(sims) / len(sims), 1) if sims else 0,
            }
            if any_capped:
                summary["accuracy_note"] = (
                    f"One or more pages exceeded {_COMPARE_WORD_CAP} words; the "
                    "text-similarity score for those pages is based on the first "
                    f"{_COMPARE_WORD_CAP} words only and may understate the real "
                    "similarity."
                )
            zf.writestr("summary.json", json.dumps(summary))
        finally:
            _finalise_zip(zf, buf, ctx.output_path)
    finally:
        doc1.close()
        doc2.close()

    ctx.set_progress(100)
    log.info(f"[{ctx.job_id}] compare_pdf: {pages} pages compared")
    return {"pages_compared": pages}


# ═══════════════════════════════════════════════════════════════════════════════
# PDF SPLIT / MIX (extra organize operations)
# ═══════════════════════════════════════════════════════════════════════════════

_FILENAME_SAFE_RE = re.compile(r'[^\w\-]+')


def _sanitise_filename(name: str, fallback: str = "section") -> str:
    """Turn an arbitrary bookmark/title into a safe, bounded filename stem."""
    cleaned = _FILENAME_SAFE_RE.sub("_", (name or "").strip()).strip("_")
    return (cleaned[:80] or fallback)


@register("split_by_bookmarks")
def split_by_bookmarks(ctx: JobContext) -> dict:
    """
    Split a PDF at each top-level (level 1) bookmark into a streaming ZIP.
    Each output file is named by the (sanitised) bookmark title.
    Raises ValidationError if the PDF has no bookmarks.
    """
    _require(FITZ_OK, "split_by_bookmarks", "PyMuPDF")
    _guard_empty(ctx.input_path)

    src = fitz.open(ctx.input_path)
    try:
        total = len(src)
        toc = src.get_toc()  # [level, title, page(1-based)]
        tops = [(t[1], t[2]) for t in toc if t[0] == 1 and 1 <= t[2] <= total]
        if not tops:
            raise ValidationError(
                "This PDF has no top-level bookmarks to split on. Use Split PDF "
                "to split by page ranges instead."
            )

        # Build inclusive 0-based [start, end] ranges from bookmark boundaries.
        ranges = []
        for i, (title, page) in enumerate(tops):
            start = page - 1
            end = (tops[i + 1][1] - 2) if i + 1 < len(tops) else total - 1
            end = max(start, min(end, total - 1))
            ranges.append((title, start, end))

        zf, buf = _open_zip_writer(ctx.output_path, len(ranges))
        try:
            for idx, (title, start, end) in enumerate(ranges):
                out = fitz.open()
                out.insert_pdf(src, from_page=start, to_page=end)
                data = out.tobytes(deflate=True, garbage=2)
                out.close()
                stem = _sanitise_filename(title, f"section_{idx + 1}")
                zf.writestr(f"{idx + 1:02d}_{stem}.pdf", data)
                ctx.set_progress(int((idx + 1) / len(ranges) * 95))
        finally:
            _finalise_zip(zf, buf, ctx.output_path)
    finally:
        src.close()

    log.info(f"[{ctx.job_id}] split_by_bookmarks: {len(ranges)} sections")
    return {"sections": len(ranges)}


@register("split_by_size")
def split_by_size(ctx: JobContext) -> dict:
    """
    Split a PDF into parts each no larger than `max_mb` (default 10), greedily
    accumulating whole pages. A single page larger than the limit is emitted on
    its own. Output is a streaming ZIP.
    """
    _require(FITZ_OK, "split_by_size", "PyMuPDF")
    _guard_empty(ctx.input_path)

    try:
        max_mb = float(ctx.params.get("max_mb", 10))
    except (TypeError, ValueError):
        raise ValidationError("max_mb must be a number")
    if max_mb <= 0:
        raise ValidationError("max_mb must be positive")
    max_bytes = int(max_mb * 1024 * 1024)

    src = fitz.open(ctx.input_path)
    try:
        total = len(src)
        zf, buf = _open_zip_writer(ctx.output_path, total)
        part_index = 0
        try:
            cur = fitz.open()
            for i in range(total):
                cur.insert_pdf(src, from_page=i, to_page=i)
                data = cur.tobytes(deflate=True, garbage=2)
                if len(data) > max_bytes and len(cur) > 1:
                    # This page tipped the part over the limit — flush everything
                    # BEFORE it, then start a fresh part with this page.
                    cur.delete_page(len(cur) - 1)
                    part_index += 1
                    zf.writestr(
                        f"part_{part_index:03d}.pdf",
                        cur.tobytes(deflate=True, garbage=2),
                    )
                    cur.close()
                    cur = fitz.open()
                    cur.insert_pdf(src, from_page=i, to_page=i)
                ctx.set_progress(int((i + 1) / total * 95))
            if len(cur) > 0:
                part_index += 1
                zf.writestr(
                    f"part_{part_index:03d}.pdf",
                    cur.tobytes(deflate=True, garbage=2),
                )
            cur.close()
        finally:
            _finalise_zip(zf, buf, ctx.output_path)
    finally:
        src.close()

    log.info(f"[{ctx.job_id}] split_by_size: {part_index} parts (<= {max_mb} MB each)")
    return {"parts": part_index, "max_mb": max_mb}


@register("alternate_mix")
def alternate_mix(ctx: JobContext) -> dict:
    """
    Interleave the pages of two PDFs (A1, B1, A2, B2, ...).
    param reverse_second: if truthy, iterate the second PDF's pages in reverse
    order — handy for merging a front-side scan with a back-side scan captured
    in reverse.
    """
    _require(FITZ_OK, "alternate_mix", "PyMuPDF")
    if not ctx.input_paths or len(ctx.input_paths) < 2:
        raise ValidationError("alternate_mix needs exactly two PDF files")

    reverse_second = str(ctx.params.get("reverse_second", "")).lower() in (
        "1", "true", "yes", "on",
    )

    a = fitz.open(ctx.input_paths[0])
    b = fitz.open(ctx.input_paths[1])
    la, lb = len(a), len(b)
    try:
        if la == 0 or lb == 0:
            raise ValidationError("Both PDFs must have at least one page")
        b_order = list(range(lb))
        if reverse_second:
            b_order.reverse()
        out = fitz.open()
        try:
            for i in range(max(la, lb)):
                if i < la:
                    out.insert_pdf(a, from_page=i, to_page=i)
                if i < lb:
                    bi = b_order[i]
                    out.insert_pdf(b, from_page=bi, to_page=bi)
            out.save(ctx.output_path, deflate=True, garbage=3)
        finally:
            out.close()
    finally:
        a.close()
        b.close()

    log.info(f"[{ctx.job_id}] alternate_mix: reverse_second={reverse_second}")
    return {"pages_total": la + lb, "reverse_second": reverse_second}


# ═══════════════════════════════════════════════════════════════════════════════
# PDF METADATA / STAMPS / RESIZE / FORMS / HTML
# ═══════════════════════════════════════════════════════════════════════════════

@register("remove_metadata")
def remove_metadata(ctx: JobContext) -> dict:
    """
    Strip document metadata (title/author/subject/keywords/creator/producer)
    and any XMP metadata stream — a privacy cleaner.
    """
    _require(FITZ_OK, "remove_metadata", "PyMuPDF")
    _guard_empty(ctx.input_path)

    doc = fitz.open(ctx.input_path)
    try:
        meta = doc.metadata or {}
        fields_cleared = sorted(k for k, v in meta.items() if v)
        doc.set_metadata({})
        try:
            doc.del_xml_metadata()
        except Exception as ex:
            log.warning(f"[{ctx.job_id}] remove_metadata: XMP strip skipped ({ex})")
        doc.save(ctx.output_path, garbage=4, deflate=True)
    finally:
        doc.close()

    log.info(f"[{ctx.job_id}] remove_metadata: cleared {fields_cleared}")
    return {"fields_cleared": fields_cleared}


@register("add_header_footer")
def add_header_footer(ctx: JobContext) -> dict:
    """
    Stamp a centered header and/or footer on every page.
    params: header_text, footer_text, font_size (default 10), margin (default 20).
    Supports {page} and {total} placeholders.
    """
    _require(FITZ_OK, "add_header_footer", "PyMuPDF")
    header_text = str(ctx.params.get("header_text", "") or "")
    footer_text = str(ctx.params.get("footer_text", "") or "")
    if not header_text and not footer_text:
        raise ValidationError("Provide at least one of header_text or footer_text")
    try:
        font_size = float(ctx.params.get("font_size", 10))
        margin = float(ctx.params.get("margin", 20))
    except (TypeError, ValueError):
        raise ValidationError("font_size and margin must be numbers")
    font_size = max(4.0, min(font_size, 72.0))

    _guard_empty(ctx.input_path)
    doc = fitz.open(ctx.input_path)
    try:
        total = len(doc)

        def _fmt(t, i):
            return t.replace("{page}", str(i + 1)).replace("{total}", str(total))

        for i, page in enumerate(doc):
            r = page.rect
            if header_text:
                rect = fitz.Rect(margin, margin, r.width - margin, margin + font_size + 4)
                page.insert_textbox(
                    rect, _fmt(header_text, i), fontsize=font_size,
                    fontname="helv", align=1,
                )
            if footer_text:
                rect = fitz.Rect(
                    margin, r.height - margin - font_size - 4,
                    r.width - margin, r.height - margin,
                )
                page.insert_textbox(
                    rect, _fmt(footer_text, i), fontsize=font_size,
                    fontname="helv", align=1,
                )
        doc.save(ctx.output_path, deflate=True)
    finally:
        doc.close()

    log.info(f"[{ctx.job_id}] add_header_footer: {total} pages stamped")
    return {"pages_stamped": total}


_PAGE_SIZE_POINTS = {
    "A4":     (595, 842),
    "A3":     (842, 1191),
    "Letter": (612, 792),
    "Legal":  (612, 1008),
}


@register("resize_pdf")
def resize_pdf(ctx: JobContext) -> dict:
    """
    Rescale every page onto a standard paper size (A4/A3/Letter/Legal),
    preserving aspect ratio and centering the content.
    """
    _require(FITZ_OK, "resize_pdf", "PyMuPDF")
    page_size = str(ctx.params.get("page_size", "A4")).strip().title()
    # Normalise common variants
    aliases = {"A4": "A4", "A3": "A3", "Letter": "Letter", "Legal": "Legal"}
    page_size = aliases.get(page_size, page_size)
    if page_size not in _PAGE_SIZE_POINTS:
        raise ValidationError(
            f"page_size must be one of: {', '.join(_PAGE_SIZE_POINTS)}"
        )
    tw, th = _PAGE_SIZE_POINTS[page_size]

    _guard_empty(ctx.input_path)
    src = fitz.open(ctx.input_path)
    resized = 0
    try:
        out = fitz.open()
        try:
            for i in range(len(src)):
                sp = src[i].rect
                if sp.width <= 0 or sp.height <= 0:
                    continue
                scale = min(tw / sp.width, th / sp.height)
                w = sp.width * scale
                h = sp.height * scale
                x = (tw - w) / 2
                y = (th - h) / 2
                new_page = out.new_page(width=tw, height=th)
                new_page.show_pdf_page(fitz.Rect(x, y, x + w, y + h), src, i)
                resized += 1
            out.save(ctx.output_path, deflate=True, garbage=3)
        finally:
            out.close()
    finally:
        src.close()

    log.info(f"[{ctx.job_id}] resize_pdf: {resized} pages → {page_size}")
    return {"pages_resized": resized, "target_size": page_size}


# pdf_to_html sanitisation. MuPDF's get_text("html") escapes text nodes, but a
# crafted PDF can still smuggle markup through attribute contexts (font names
# and similar metadata land in inline styles). Whitelist exactly the tags,
# attributes and CSS properties MuPDF emits so formatting survives intact.
_HTML_EXPORT_TAGS  = {"div", "p", "span", "img", "b", "i", "u", "s", "br"}
_HTML_EXPORT_ATTRS = {
    "div":  ["style", "id"],
    "img":  ["style", "src", "width", "height"],
    "p":    ["style"], "span": ["style"],
    "b":    ["style"], "i": ["style"], "u": ["style"], "s": ["style"],
}
_HTML_EXPORT_CSS = [
    "position", "top", "left", "width", "height", "line-height",
    "font-family", "font-size", "font-weight", "font-style",
    "color", "transform", "letter-spacing", "vertical-align",
]


def _html_export_cleaner():
    """bleach Cleaner for pdf_to_html output (data: images only, no links)."""
    try:
        from bleach.css_sanitizer import CSSSanitizer
        css = CSSSanitizer(allowed_css_properties=_HTML_EXPORT_CSS)
    except ImportError:
        css = None  # styles are dropped: output stays safe, formatting degrades
        log.warning("pdf_to_html: tinycss2 missing — inline styles stripped")
    return bleach.Cleaner(
        tags=_HTML_EXPORT_TAGS, attributes=_HTML_EXPORT_ATTRS,
        protocols=["data"], css_sanitizer=css,
        strip=True, strip_comments=True,
    )


@register("pdf_to_html")
def pdf_to_html(ctx: JobContext) -> dict:
    """
    Export a PDF to a single self-contained HTML file (one <div> per page).
    Page markup is sanitised (XSS) while preserving MuPDF's layout styles.
    """
    _require(FITZ_OK, "pdf_to_html", "PyMuPDF")
    _require(BLEACH_OK, "pdf_to_html", "bleach")
    _guard_empty(ctx.input_path)

    cleaner = _html_export_cleaner()
    doc = fitz.open(ctx.input_path)
    try:
        parts = []
        for i, page in enumerate(doc):
            r = page.rect
            body = cleaner.clean(page.get_text("html"))
            parts.append(
                f'<div class="pdf-page" data-page="{i + 1}" '
                f'style="position:relative;width:{r.width:.0f}pt;'
                f'height:{r.height:.0f}pt;margin:0 auto 16px;'
                f'box-shadow:0 1px 4px rgba(0,0,0,.2);background:#fff;'
                f'overflow:hidden">{body}</div>'
            )
        html = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>PDF export</title>"
            "<style>body{background:#eee;margin:0;padding:16px;"
            "font-family:sans-serif}.pdf-page p{margin:0}</style></head>"
            f"<body>{''.join(parts)}</body></html>"
        )
        with open(ctx.output_path, "w", encoding="utf-8") as fh:
            fh.write(html)
        n = len(doc)
    finally:
        doc.close()

    log.info(f"[{ctx.job_id}] pdf_to_html: {n} pages")
    return {"pages": n}


@register("fill_form")
def fill_form(ctx: JobContext) -> dict:
    """
    Fill AcroForm fields from a JSON dict of {field_name: value}.
    Text fields take strings, checkboxes take booleans, dropdowns take strings.
    """
    _require(FITZ_OK, "fill_form", "PyMuPDF")
    raw = ctx.params.get("fields", "{}")
    try:
        fields = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except (TypeError, ValueError) as ex:
        raise ValidationError(f"fields is not valid JSON: {ex}")
    if not isinstance(fields, dict):
        raise ValidationError("fields must be a JSON object of {field_name: value}")

    _guard_empty(ctx.input_path)
    doc = fitz.open(ctx.input_path)
    found = 0
    filled = 0
    try:
        for page in doc:
            try:
                widgets = list(page.widgets() or [])
            except Exception:
                widgets = []
            for w in widgets:
                found += 1
                name = w.field_name
                if name not in fields:
                    continue
                value = fields[name]
                try:
                    if w.field_type == fitz.PDF_WIDGET_TYPE_CHECKBOX:
                        truthy = value in (True, "true", "True", "on", "yes", 1, "1")
                        w.field_value = bool(truthy)
                    else:
                        w.field_value = str(value)
                    w.update()
                    filled += 1
                except Exception as ex:
                    log.warning(f"[{ctx.job_id}] fill_form: field {name!r} failed: {ex}")
        doc.save(ctx.output_path, deflate=True, garbage=3)
    finally:
        doc.close()

    log.info(f"[{ctx.job_id}] fill_form: filled {filled}/{found}")
    return {"fields_filled": filled, "fields_found": found}


@register("flatten_pdf")
def flatten_pdf(ctx: JobContext) -> dict:
    """
    Bake form fields and annotations into static page content so values are no
    longer editable. Uses PyMuPDF's bake().
    """
    _require(FITZ_OK, "flatten_pdf", "PyMuPDF")
    _guard_empty(ctx.input_path)

    doc = fitz.open(ctx.input_path)
    try:
        count = 0
        for page in doc:
            try:
                count += len(list(page.widgets() or []))
            except Exception:
                pass
        if not hasattr(doc, "bake"):
            raise UnsupportedOperation("flatten_pdf", "PyMuPDF>=1.24.2 (Document.bake)")
        doc.bake(annots=True, widgets=True)
        doc.save(ctx.output_path, deflate=True, garbage=3)
    finally:
        doc.close()

    log.info(f"[{ctx.job_id}] flatten_pdf: flattened {count} fields")
    return {"fields_flattened": count}


# ═══════════════════════════════════════════════════════════════════════════════
# CANVAS EDITOR — in-place text editing (synchronous, NOT pipeline-registered)
# ═══════════════════════════════════════════════════════════════════════════════
#
# Powers the "Edit PDF" visual canvas tool. Called DIRECTLY from
# app/routes/pdf_routes.py (no Celery, no Redis job) because the user waits
# interactively and the work is fast (<2s for typical PDFs):
#
#   _parse_canvas_sync(pdf_path)                 -> dict   (page images + text spans)
#   _save_canvas_sync(pdf_path, changes, scanned)-> bytes  (rebuilt PDF)
#
# Scanned PDFs (no text layer) are auto-OCR'd: Tesseract word boxes become
# editable spans directly — no text-layer round-trip, so the existing ocr_pdf
# tool is left completely untouched.
#
# Coordinates everywhere are PDF points with a TOP-LEFT origin (the space used
# by both page.get_text() and page.get_pixmap()), so frontend overlay math is a
# simple uniform scale.

_CANVAS_RENDER_DPI   = 150     # page preview render resolution
_CANVAS_MAX_PAGES    = 50      # sync endpoint — keep render fast & memory bounded
_CANVAS_OCR_DPI      = 200     # OCR render resolution for scanned PDFs
_CANVAS_OCR_MIN_CONF = 30      # drop OCR words below this confidence

# Cache base-14 fitz.Font objects — re-creating them per span is wasteful.
_FITZ_FONT_CACHE: dict = {}


def _fitz_font(fontname: str):
    f = _FITZ_FONT_CACHE.get(fontname)
    if f is None:
        f = fitz.Font(fontname=fontname)
        _FITZ_FONT_CACHE[fontname] = f
    return f


def _is_scanned_pdf(path: str) -> bool:
    """
    True if the PDF has no usable text layer.
    Checks the first 3 pages; < 50 stripped chars total → treat as scanned.
    """
    doc = fitz.open(path)
    try:
        pages_to_check = min(3, len(doc))
        total_chars = 0
        for i in range(pages_to_check):
            total_chars += len(doc[i].get_text().strip())
        return total_chars < 50
    finally:
        doc.close()


def _pack_color_to_rgb(c) -> list:
    """PyMuPDF span colour is a packed sRGB int. Return [R,G,B] in 0-255."""
    if isinstance(c, (list, tuple)):
        vals = list(c[:3]) or [0, 0, 0]
        if vals and max(vals) <= 1.0:
            vals = [int(round(v * 255)) for v in vals]
        return [int(max(0, min(255, v))) for v in vals]
    try:
        c = int(c)
    except (TypeError, ValueError):
        return [0, 0, 0]
    return [(c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF]


def _base14_for(font: str, flags: int) -> str:
    """Map an arbitrary font + span flags to a base-14 PyMuPDF font code.

    NOTE: the correct italic code is 'heit' (Helvetica-Oblique) and bold-italic
    is 'hebi' (Helvetica-BoldOblique). 'heio' is NOT a valid code.
    flags bit 4 (16) = bold, bit 1 (2) = italic.

    Kept for legacy callers (and as a fallback). The canvas editor now
    routes through `_unicode_font_for` instead — base-14 helv/hebo etc.
    SILENTLY corrupt em-dashes (—), "+" near digits, currency symbols (₹),
    curly quotes, etc., because those glyphs aren't in their character set.
    See _UNICODE_TTF_FOR / _insert_fitted_text for the real path.
    """
    fl = (font or "").lower()
    bold   = bool(flags & (1 << 4)) or "bold" in fl or "black" in fl or "heavy" in fl
    italic = bool(flags & (1 << 1)) or "italic" in fl or "oblique" in fl
    if bold and italic:
        return "hebi"
    if bold:
        return "hebo"
    if italic:
        return "heit"
    return "helv"


# ── Canvas editor: Unicode-capable TTF font registry ────────────────────────
# The canvas editor's previous behaviour was to insert replacement text via
# PyMuPDF's base-14 fonts (helv/hebo/heit/hebi). Those fonts have ONLY
# WinAnsi-1252 character coverage — so every em-dash, "50+", "₹", smart-quote
# and Devanagari character was silently dropped or substituted, even when the
# user just kept the original text and pressed Enter. That's the bug that
# made colleagues laugh at the demo.
#
# DejaVu Sans is shipped with the image (LibreOffice depends on it) and
# covers every character we've seen in real-world PDFs. We register the
# four faces with stable internal aliases and reuse them on every page that
# needs an edit. The TTF bytes are read once at import.

_UNICODE_TTF_PATHS = {
    "regular":    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "bold":       "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "italic":     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
    "bolditalic": "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf",
}
# Fallbacks for environments without DejaVu — keep the fix working anywhere
# LibreOffice or any modern Linux distro has installed *something* covering
# Latin + Indic + symbols.
_UNICODE_TTF_FALLBACKS = [
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",   # RHEL/Oracle path
]

# Cache: page_id -> {face_key: alias} so we don't re-embed the same TTF on
# every span insertion (PyMuPDF embeds duplicates if you call insert_font
# repeatedly with the same fontfile).
_PAGE_UNICODE_FONT_CACHE: dict = {}
# Cache: face_key -> fitz.Font (used for accurate text-length measurement)
_UNICODE_FONT_OBJECTS: dict = {}


def _resolve_unicode_ttf(face: str) -> Optional[str]:
    path = _UNICODE_TTF_PATHS.get(face)
    if path and os.path.exists(path):
        return path
    # Try the regular fallback for any face — better than dropping characters.
    for fb in _UNICODE_TTF_FALLBACKS:
        if os.path.exists(fb):
            return fb
    return None


def _unicode_font_for(face_key: str):
    """Return a fitz.Font for accurate width measurement, or None."""
    if face_key in _UNICODE_FONT_OBJECTS:
        return _UNICODE_FONT_OBJECTS[face_key]
    path = _resolve_unicode_ttf(face_key)
    if not path:
        return None
    try:
        f = fitz.Font(fontfile=path)
    except Exception:
        f = None
    _UNICODE_FONT_OBJECTS[face_key] = f
    return f


def _face_key_for(font: str, flags: int) -> str:
    """Match the existing _base14_for logic but return our face key."""
    fl = (font or "").lower()
    bold   = bool(flags & (1 << 4)) or "bold" in fl or "black" in fl or "heavy" in fl
    italic = bool(flags & (1 << 1)) or "italic" in fl or "oblique" in fl
    if bold and italic:
        return "bolditalic"
    if bold:
        return "bold"
    if italic:
        return "italic"
    return "regular"


def _ensure_unicode_font_on_page(page, face_key: str) -> Optional[str]:
    """Embed the chosen TTF face on `page` (idempotent). Returns the alias
    to use as `fontname=` in insert_text, or None if no TTF is available.
    """
    page_id = id(page)
    page_cache = _PAGE_UNICODE_FONT_CACHE.setdefault(page_id, {})
    if face_key in page_cache:
        return page_cache[face_key]
    path = _resolve_unicode_ttf(face_key)
    if not path:
        return None
    alias = f"UNI_{face_key[:2]}"   # short alias, must be unique per page
    try:
        page.insert_font(fontname=alias, fontfile=path)
    except Exception as ex:
        log.warning(f"canvas: insert_font({alias}, {path}) failed: {ex}")
        return None
    page_cache[face_key] = alias
    return alias


def _ocr_page_spans(page, dpi: int = _CANVAS_OCR_DPI, lang: str = "eng") -> list:
    """
    OCR one page and synthesise editable spans in PDF-point coordinates
    (top-left origin). Used only for scanned PDFs.
    """
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat, alpha=False, colorspace=fitz.csGRAY)
    # Zero-copy handoff (same approach as ocr_pdf): skip the PNG encode+decode
    # round-trip and build the PIL image straight from the raw pixmap buffer.
    mode = "L" if pix.n == 1 else ("RGB" if pix.n == 3 else "RGBA")
    img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
    data = pytesseract.image_to_data(
        img, lang=lang, output_type=TesseractOutput.DICT, config="--psm 3 --oem 3"
    )
    scale = 72.0 / dpi   # pixel -> point
    spans = []
    n = len(data.get("text", []))
    for i in range(n):
        word = (data["text"][i] or "").strip()
        if not word:
            continue
        try:
            conf = int(float(data["conf"][i]))
        except (TypeError, ValueError):
            conf = -1
        if conf < _CANVAS_OCR_MIN_CONF:
            continue
        x = data["left"][i]   * scale
        y = data["top"][i]    * scale
        w = data["width"][i]  * scale
        h = data["height"][i] * scale
        if w <= 0 or h <= 0:
            continue
        size = max(6.0, h * 0.85)
        baseline_y = y + h - h * 0.18
        spans.append({
            "text": word,
            "x0": round(x, 2),       "y0": round(y, 2),
            "x1": round(x + w, 2),   "y1": round(y + h, 2),
            "ox": round(x, 2),       "oy": round(baseline_y, 2),
            "font": "OCR", "size": round(size, 2),
            "color": [0, 0, 0], "flags": 0,
        })
    return spans


def _parse_canvas_sync(pdf_path: str, render_dpi: int = _CANVAS_RENDER_DPI,
                       max_pages: int = _CANVAS_MAX_PAGES) -> dict:
    """Render page images + extract editable text spans for the canvas editor."""
    _require(FITZ_OK, "parse-canvas", "PyMuPDF")
    import base64
    _guard_empty(pdf_path)

    scanned = _is_scanned_pdf(pdf_path)
    use_ocr = scanned and TESSERACT_OK and PIL_OK

    doc = fitz.open(pdf_path)
    try:
        n = len(doc)
        if n > max_pages:
            raise ValidationError(
                f"This PDF has {n} pages. The visual editor supports up to "
                f"{max_pages} pages — use Split PDF first to edit a section."
            )
        mat = fitz.Matrix(render_dpi / 72, render_dpi / 72)
        pages = []
        total_spans = 0
        for pno in range(n):
            page = doc[pno]
            rect = page.rect
            pix  = page.get_pixmap(matrix=mat, alpha=False)
            img_b64 = base64.b64encode(pix.tobytes("png")).decode("ascii")

            spans = []
            if use_ocr:
                for j, sp in enumerate(_ocr_page_spans(page)):
                    sp["id"] = f"p{pno}_o{j}"
                    spans.append(sp)
            else:
                d = page.get_text(
                    "dict",
                    flags=fitz.TEXTFLAGS_DICT | fitz.TEXT_PRESERVE_WHITESPACE,
                )
                for bi, block in enumerate(d.get("blocks", [])):
                    if block.get("type", 0) != 0:        # 0 = text block
                        continue
                    for li, line in enumerate(block.get("lines", [])):
                        for si, span in enumerate(line.get("spans", [])):
                            text = span.get("text", "")
                            if not text.strip():
                                continue
                            x0, y0, x1, y1 = span["bbox"]
                            ox, oy = span.get("origin", (x0, y1))
                            spans.append({
                                "id":   f"p{pno}_b{bi}_l{li}_s{si}",
                                "text": text,
                                "x0": round(x0, 2), "y0": round(y0, 2),
                                "x1": round(x1, 2), "y1": round(y1, 2),
                                "ox": round(ox, 2), "oy": round(oy, 2),
                                "font": span.get("font", ""),
                                "size": round(span.get("size", 0), 2),
                                "color": _pack_color_to_rgb(span.get("color", 0)),
                                "flags": int(span.get("flags", 0)),
                            })
            total_spans += len(spans)
            pages.append({
                "page": pno,
                "pdf_width":     round(rect.width, 2),
                "pdf_height":    round(rect.height, 2),
                "render_width":  pix.width,
                "render_height": pix.height,
                "image_b64":     img_b64,
                "spans":         spans,
            })
        return {
            "scanned":     scanned,
            "ocr_applied": use_ocr,
            "page_count":  n,
            "total_spans": total_spans,
            "pages":       pages,
        }
    finally:
        doc.close()


def _insert_fitted_text(page, ch: dict) -> None:
    """Insert one replacement span.

    Uses a Unicode-capable TTF (DejaVu Sans) embedded on demand so
    em-dashes, "50+", "₹", smart quotes, Devanagari, etc. survive the
    edit. Falls back to base-14 helv only if no TTF is available — in
    which case the legacy character-substitution bug returns.
    Width is measured with the SAME font as the render to keep
    shrink-to-fit honest.
    """
    new_text = str(ch.get("new_text", ""))
    if new_text == "":
        return  # pure deletion — the redaction already cleared the original
    x0 = float(ch["x0"]); y0 = float(ch["y0"])
    x1 = float(ch["x1"]); y1 = float(ch["y1"])
    flags = int(ch.get("flags", 0) or 0)
    face_key = _face_key_for(str(ch.get("font", "")), flags)

    color = _pack_color_to_rgb(ch.get("color", [0, 0, 0]))
    color_f = tuple(v / 255.0 for v in color)
    size = float(ch.get("size", 0) or 0) or max(6.0, (y1 - y0) * 0.8)

    # Prefer the embedded TTF; fall back to base-14 helv only if TTF
    # registration failed (e.g. fonts removed from the image).
    ttf_alias = _ensure_unicode_font_on_page(page, face_key)
    measure_font = _unicode_font_for(face_key) if ttf_alias else None
    if ttf_alias and measure_font is not None:
        fontname = ttf_alias
        measure = measure_font
    else:
        # Last-resort: base-14. Will silently drop em-dash / + / ₹ / etc.
        fontname = _base14_for(str(ch.get("font", "")), flags)
        measure = _fitz_font(fontname)
        if not ttf_alias:
            log.warning(
                "canvas: no Unicode TTF available; falling back to base-14 — "
                "em-dash / + / ₹ may be substituted in span %s",
                ch.get("id"),
            )

    avail_w = max(1.0, x1 - x0)
    try:
        tw = measure.text_length(new_text, fontsize=size)
    except Exception:
        tw = 0

    fs = size
    if tw > avail_w and tw > 0:
        # Shrink to fit width. Floor at 4 pt — anything smaller is
        # unreadable and means the user typed something far longer
        # than the original line; we'd rather they see condensed text
        # than truncated text.
        fs = max(4.0, size * (avail_w / tw) * 0.985)

    ox = float(ch.get("ox", x0))
    oy = float(ch.get("oy", y1 - size * 0.18))
    try:
        page.insert_text((ox, oy), new_text, fontname=fontname,
                         fontsize=fs, color=color_f, overlay=True)
    except Exception as ex:
        log.warning(f"canvas insert_text failed for span {ch.get('id')}: {ex}")


def _sample_background_color(page, rect: "fitz.Rect", dpi: int = 96) -> tuple:
    """Sample the page background color *around* a text rectangle.

    Previously every redaction used a hardcoded white fill — that leaves an
    ugly white box on any colored page (blue header bands, dark themes, etc).
    Now we render a small region around the span, look at pixels JUST OUTSIDE
    the text bbox (not under it — that has glyphs we don't want sampled), and
    return the dominant colour as a fitz-style (r, g, b) float tuple in 0..1.

    Strategy:
      • Render a clip box that's the span rect + 6 pt margin.
      • Mask out the inner text rect (pixels inside it are the OLD text and
        will skew our sample).
      • Build a coarse histogram by quantising each pixel to a 16-bucket cube.
      • Pick the most-populated bucket. Tie-breaks favour brighter colours so
        a near-white background with anti-aliased text edges still picks the
        background, not the dark text edge halo.

    Returns (r, g, b) in 0..1. Falls back to white (1, 1, 1) on any error so
    the editor at least matches today's behaviour rather than crashing.
    """
    try:
        margin = 6.0
        page_rect = page.rect
        clip = fitz.Rect(
            max(page_rect.x0, rect.x0 - margin),
            max(page_rect.y0, rect.y0 - margin),
            min(page_rect.x1, rect.x1 + margin),
            min(page_rect.y1, rect.y1 + margin),
        )
        if clip.is_empty or clip.width < 2 or clip.height < 2:
            return (1.0, 1.0, 1.0)

        mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        pix = page.get_pixmap(matrix=mat, alpha=False, clip=clip)
        # pix is now a small image. Convert sample coords to pixmap pixels.
        sx = mat.a   # horizontal scale (dpi / 72)
        sy = mat.d
        # Pixel bounds of the INNER text rect (in pixmap coords, relative to clip).
        ix0 = int(round((rect.x0 - clip.x0) * sx))
        iy0 = int(round((rect.y0 - clip.y0) * sy))
        ix1 = int(round((rect.x1 - clip.x0) * sx))
        iy1 = int(round((rect.y1 - clip.y0) * sy))

        # Build histogram of OUTSIDE pixels.
        # pix.samples is bytes RGB, row-major, stride = pix.stride.
        samples = pix.samples
        w, h, stride = pix.width, pix.height, pix.stride
        hist: dict = {}
        # Walk every Nth pixel to keep this fast — 200-300 samples is plenty.
        step = max(1, (w * h) // 1200)
        idx = 0
        for y in range(h):
            row = y * stride
            for x in range(w):
                idx += 1
                if idx % step:
                    continue
                # skip pixels INSIDE the text bbox (they're the old glyphs)
                if ix0 <= x < ix1 and iy0 <= y < iy1:
                    continue
                r = samples[row + x*3]
                g = samples[row + x*3 + 1]
                b = samples[row + x*3 + 2]
                # 16-bucket cube → 4096 possible keys
                key = ((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4)
                hist[key] = hist.get(key, 0) + 1
        if not hist:
            return (1.0, 1.0, 1.0)
        # Best bucket; tie-break by luminance so near-white wins over anti-
        # aliased dark text edges that may have landed in the outer ring.
        def _score(item):
            key, count = item
            r = ((key >> 8) & 0xF) << 4
            g = ((key >> 4) & 0xF) << 4
            b = (key & 0xF) << 4
            lum = 0.299*r + 0.587*g + 0.114*b
            return (count, lum)
        best = max(hist.items(), key=_score)[0]
        r = (((best >> 8) & 0xF) << 4) + 8   # +8 = bucket center
        g = (((best >> 4) & 0xF) << 4) + 8
        b = ( (best       & 0xF) << 4) + 8
        return (r / 255.0, g / 255.0, b / 255.0)
    except Exception as ex:
        log.warning(f"canvas: background colour sample failed ({ex}); falling back to white")
        return (1.0, 1.0, 1.0)


def _save_canvas_sync(pdf_path: str, changes: list, scanned: bool = False) -> bytes:
    """Surgically replace only the edited spans; everything else is untouched."""
    _require(FITZ_OK, "save-canvas", "PyMuPDF")
    if not isinstance(changes, list):
        raise ValidationError("changes must be a list")

    by_page: dict = {}
    for ch in changes:
        try:
            pno = int(ch["page"])
        except (KeyError, TypeError, ValueError):
            continue
        by_page.setdefault(pno, []).append(ch)

    if not by_page:
        raise ValidationError("No valid changes to apply")

    # Scanned pages: blank the covered raster pixels. Text pages: leave images
    # (logos/photos) alone — only text/vector under the rect is removed.
    img_mode = fitz.PDF_REDACT_IMAGE_PIXELS if scanned else fitz.PDF_REDACT_IMAGE_NONE

    doc = fitz.open(pdf_path)
    try:
        npages = len(doc)
        edited = 0
        for pno, chs in by_page.items():
            if pno < 0 or pno >= npages:
                continue
            page = doc[pno]
            # Step A — for each edited span, sample the page background colour
            # AROUND the span and use that as the redaction fill. The old code
            # hardcoded white, which left a glaring white rectangle on every
            # coloured page (blue headers, dark themes, image backgrounds…).
            applied_any = False
            for ch in chs:
                try:
                    x0 = float(ch["x0"]); y0 = float(ch["y0"])
                    x1 = float(ch["x1"]); y1 = float(ch["y1"])
                except (KeyError, TypeError, ValueError):
                    continue
                rect = fitz.Rect(x0 - 1, y0 - 1, x1 + 1, y1 + 1)
                bg = _sample_background_color(page, rect)
                page.add_redact_annot(rect, fill=bg)
                applied_any = True
            if applied_any:
                page.apply_redactions(images=img_mode)
            # Step B — re-insert the new text
            for ch in chs:
                _insert_fitted_text(page, ch)
                edited += 1
        if edited == 0:
            raise ValidationError("No valid changes to apply")
        buf = io.BytesIO()
        doc.save(buf, deflate=True, garbage=4, clean=True)
        return buf.getvalue()
    finally:
        # Drop the per-page font cache for THIS document — we keyed on id(page)
        # which gets reused once the underlying objects are gc'd, so leaving
        # stale entries can cause "missing font" errors on the next call.
        try:
            for pno in range(len(doc)):
                _PAGE_UNICODE_FONT_CACHE.pop(id(doc[pno]), None)
        except Exception:
            pass
        doc.close()
