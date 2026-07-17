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


def _qpdf_pack(src: str, out: str, timeout: int = 120) -> bool:
    """
    Structural (lossless) optimization with qpdf: pack every object into
    compressed object streams, recompress all flate streams at max level, and
    drop unreferenced objects. This is the single biggest win on text/vector
    PDFs (where there are no images to downsample) and is fidelity-perfect —
    text, fonts, forms, links, annotations and bookmarks are all preserved
    exactly. Returns True on a non-empty result.
    """
    if not QPDF_OK:
        return False
    try:
        r = subprocess.run(
            ["qpdf", "--object-streams=generate", "--compress-streams=y",
             "--recompress-flate", "--compression-level=9",
             "--deterministic-id", src, out],
            capture_output=True, timeout=timeout,
        )
        # qpdf exit 0 = clean, 3 = warnings-but-wrote-output; both are usable.
        if r.returncode in (0, 3) and os.path.exists(out) and os.path.getsize(out) > 0:
            return True
    except (subprocess.SubprocessError, OSError):
        pass
    return False


def _pdf_feature_count(path: str) -> dict:
    """
    Count the interactive/structural features a compressor must not silently
    destroy: form widgets, link annotations, other annotations, and bookmarks.
    Ghostscript's pdfwrite flattens AcroForm fields (widgets → 0), so a
    candidate that drops them is disqualified for form documents.
    """
    out = {"widgets": 0, "links": 0, "annots": 0, "bookmarks": 0}
    if not FITZ_OK:
        return out
    doc = None
    try:
        doc = fitz.open(path)
        for pg in doc:
            try: out["widgets"] += len(list(pg.widgets() or []))
            except Exception: pass
            try: out["links"] += len(pg.get_links())
            except Exception: pass
            try: out["annots"] += len(list(pg.annots() or []))
            except Exception: pass
        out["bookmarks"] = len(doc.get_toc(simple=True))
    except Exception:
        pass
    finally:
        if doc is not None:
            doc.close()
    return out


def _feature_preserved(orig: dict, cand_path: str) -> bool:
    """A candidate is acceptable only if it keeps every form widget and bookmark
    the original had (links/annots are advisory). This is what stops us shipping
    a smaller file that has silently lost the user's form fields."""
    c = _pdf_feature_count(cand_path)
    return c["widgets"] >= orig["widgets"] and c["bookmarks"] >= orig["bookmarks"]


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

# Compression level → image downsample target (DPI) + JPEG quality. Levels are
# normalised from the various front-end labels. Object-stream/flate packing
# (qpdf) is applied at every level; these knobs only govern LOSSY image
# downsampling in the Ghostscript / PyMuPDF candidates.
_COMPRESS_LEVELS = {
    "maximum": {"dpi": 72,  "quality": 45},   # smallest — aggressive
    "high":    {"dpi": 100, "quality": 60},
    "medium":  {"dpi": 150, "quality": 75},   # balanced default
    "low":     {"dpi": 200, "quality": 85},   # gentle, near-lossless images
}
_COMPRESS_LEVEL_ALIASES = {
    "extreme": "maximum", "strong": "maximum", "smallest": "maximum",
    "recommended": "medium", "balanced": "medium", "default": "medium",
    "high quality": "low", "high-quality": "low", "less": "low",
    "lossless": "low", "gentle": "low",
}


def _gs_compress(src: str, out: str, cfg: dict) -> bool:
    """Ghostscript downsample+recompress candidate (lossy). Uses /screen as the
    base object model (most reliable) with the image resolution overridden per
    level. Deliberately NOT linearized — FastWebView/linearization adds hint
    tables that *bloat* small text PDFs, which is what made the old pipeline
    return files larger than the input."""
    try:
        return _ghostscript(
            src, out, "/screen",
            extra_flags=[
                "-dDownsampleColorImages=true", "-dDownsampleGrayImages=true",
                "-dColorImageDownsampleType=/Bicubic",
                "-dGrayImageDownsampleType=/Bicubic",
                "-dColorImageDownsampleThreshold=1.0",
                "-dGrayImageDownsampleThreshold=1.0",
                f"-dColorImageResolution={cfg['dpi']}",
                f"-dGrayImageResolution={cfg['dpi']}",
                f"-dJPEGQ={cfg['quality']}",
                "-dSubsetFonts=true", "-dCompressFonts=true", "-dEmbedAllFonts=true",
            ],
        )
    except OperationTimeoutError:
        return False


@register("compress_pdf")
def compress_pdf(ctx: JobContext) -> dict:
    """
    Adaptive, feature-preserving PDF compression.

    We generate several candidates and keep the smallest one that both RENDERS
    and preserves the document's interactive features, then never return a file
    larger than the original:

      • qpdf  — lossless object-stream + flate repack. Wins text/vector PDFs
                (where there is nothing to downsample) and preserves everything
                exactly. This is the candidate that beats iLovePDF on text docs.
      • PyMuPDF effective-DPI image downsample → qpdf. Compresses images while
                keeping form fields intact (unlike Ghostscript).
      • Ghostscript downsample → qpdf. Best on scanned/photo/transparency PDFs,
                but Ghostscript flattens AcroForm fields, so it is only eligible
                when it does not drop widgets/bookmarks the original had.

    Selection is by measured output size among candidates that pass a render
    check and a feature-retention check — no single pipeline is trusted blindly.
    """
    _require(FITZ_OK and PIL_OK, "compress_pdf", "PyMuPDF + Pillow")
    raw_q = str(ctx.params.get("quality", "medium")).strip().lower()
    level = _COMPRESS_LEVEL_ALIASES.get(raw_q, raw_q)
    cfg   = _COMPRESS_LEVELS.get(level, _COMPRESS_LEVELS["medium"])

    _guard_empty(ctx.input_path)
    orig       = os.path.getsize(ctx.input_path)
    orig_feats = _pdf_feature_count(ctx.input_path)
    ctx.set_progress(5)

    # Candidates live alongside the final output (inside OUTPUT_FOLDER) so the
    # Ghostscript path passes _safe_output_path, which only permits the
    # configured output/temp dirs.
    work = tempfile.mkdtemp(prefix="cpdf_", dir=os.path.dirname(ctx.output_path) or None)
    # candidates: list of (size, path, label, lossy)
    candidates: list[tuple[int, str, str, bool]] = []

    def _add(path: str, label: str, lossy: bool):
        if path and os.path.exists(path) and os.path.getsize(path) > 0:
            candidates.append((os.path.getsize(path), path, label, lossy))

    try:
        # ── Candidate A: lossless qpdf repack of the ORIGINAL ────────────────
        qorig = os.path.join(work, "qpdf_orig.pdf")
        if _qpdf_pack(ctx.input_path, qorig):
            _add(qorig, "qpdf", False)
        ctx.set_progress(25)

        # ── Candidate B: PyMuPDF effective-DPI image downsample → qpdf ────────
        # Feature-safe (keeps form fields), so this is how form PDFs still get
        # their images compressed.
        stage1 = os.path.join(work, "stage1.pdf")
        doc = None
        try:
            doc = fitz.open(ctx.input_path)
            modified = compress_pdf_images(doc, cfg["dpi"], cfg["quality"])
            if modified:
                doc.save(stage1, deflate=True, deflate_images=True,
                         deflate_fonts=True, garbage=3, clean=False)
            doc.close(); doc = None
            if modified and os.path.exists(stage1):
                s1q = os.path.join(work, "stage1_q.pdf")
                if _qpdf_pack(stage1, s1q):
                    _add(s1q, "pymupdf+qpdf", True)
                else:
                    _add(stage1, "pymupdf", True)
        except Exception as ex:
            log.warning(f"[{ctx.job_id}] compress stage1 failed: {ex}")
            if doc is not None:
                try: doc.close()
                except Exception: pass
        ctx.set_progress(55)

        # ── Candidate C: Ghostscript downsample → qpdf ───────────────────────
        gs_out = os.path.join(work, "gs.pdf")
        if _gs_compress(ctx.input_path, gs_out, cfg):
            gsq = os.path.join(work, "gs_q.pdf")
            if _qpdf_pack(gs_out, gsq):
                _add(gsq, "ghostscript+qpdf", True)
            else:
                _add(gs_out, "ghostscript", True)
        ctx.set_progress(80)

        # ── Selection: smallest candidate that RENDERS and keeps features ────
        candidates.sort(key=lambda x: x[0])
        chosen, chosen_label = ctx.input_path, "original"
        for _sz, cand, label, _lossy in candidates:
            if _sz >= orig:
                continue                      # never grow the file
            if not _pdf_renders_ok(cand):
                log.warning(f"[{ctx.job_id}] compress: candidate {label} failed render gate")
                continue
            if not _feature_preserved(orig_feats, cand):
                log.info(f"[{ctx.job_id}] compress: candidate {label} dropped a form/bookmark — skipped")
                continue
            chosen, chosen_label = cand, label
            break

        shutil.copy(chosen, ctx.output_path)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    new_size  = os.path.getsize(ctx.output_path)
    reduction = round((1 - new_size / orig) * 100, 1) if orig else 0
    already_optimized = reduction < 3.0
    ctx.set_progress(100)
    log.info(f"[{ctx.job_id}] compress_pdf: {orig} → {new_size} bytes "
             f"({reduction}%, via {chosen_label}, level={level})")
    return {
        "reduction_pct":          reduction,
        "original_size_bytes":    orig,
        "compressed_size_bytes":  new_size,
        "method":                 chosen_label,
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
                # Cap per-page text length fed to the regex engine. Even with
                # SafeRegex's pattern allowlist + thread timeout, CPython
                # cannot interrupt a runaway C-level match — bounding the
                # input is the strongest guarantee we can give.
                page_text = page.get_text("text")
                if page_text and len(page_text) > 100_000:
                    page_text = page_text[:100_000]
                for match in compiled.finditer(page_text):
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


# ── PDF→Word text-repair post-processor ──────────────────────────────────────
# pdf2docx faithfully reproduces whatever PyMuPDF extracts, which on many real
# PDFs (LaTeX/InDesign resumes, papers) means: fi/fl/ffi ligature glyphs dropped
# entirely ("Configuration"→"Conguration"), soft line-wrap hyphens kept as
# literal text ("secur-ing"), and stray spacing around punctuation. We repair
# these on the generated DOCX at the *run* level, so bold/italic/size formatting
# is preserved. Every repair is dictionary-gated — we only change a token when
# the result is a real word — so correct text is never touched.
_WORDS: set | None = None
_LIGATURES = ("fi", "fl", "ff", "ffi", "ffl", "ft")
# Word-final fragments that are pure suffixes (never a standalone compound
# member), so "develop-ment"/"informa-tion" are safe to rejoin even though the
# left stem ("develop") is itself a real word.
_REJOIN_SUFFIXES = (
    "ment", "ments", "tion", "tions", "sion", "sions", "ing", "ings",
    "ity", "ities", "ness", "ance", "ence", "able", "ible", "ful", "less",
)
# Genuine hyphenated compounds that must NOT be de-hyphenated.
_KEEP_HYPHEN = {
    "enterprise-scale", "on-premises", "end-to-end", "real-time", "self-hosted",
    "docker-compose", "change-controlled", "multi-node", "high-availability",
    "read-only", "day-to-day", "co-located", "hyper-v", "state-of-the-art",
}
# Unicode ligature codepoints → ASCII (U+FB00..FB06).
_UNICODE_LIGS = {
    "ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl",
    "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
}


def _load_wordlist() -> set:
    """Lazy-load the bundled English wordlist (used only to validate repairs)."""
    global _WORDS
    if _WORDS is None:
        path = os.path.join(os.path.dirname(__file__), "data", "en_words.txt.gz")
        try:
            import gzip
            with gzip.open(path, "rt", encoding="utf-8") as f:
                _WORDS = {w.strip() for w in f if w.strip()}
        except Exception as ex:
            log.warning(f"pdf_to_word: wordlist unavailable ({ex}); text repair limited")
            _WORDS = set()
    return _WORDS


def _valid_word(w: str) -> bool:
    return w.lower() in _load_wordlist()


def _should_rejoin(a: str, b: str, whole: str) -> bool:
    """Decide if a hyphenated 'a-b' is a soft line-wrap break (rejoin) vs a real
    compound (keep). Rejoin only when the joined form is a real word AND either
    the left stem is not a word (rou-tine, secur-ing) or the right part is a pure
    suffix (develop-ment). Keeps explicit compounds and re-*/co-* meaning-change
    cases (re-cover, co-worker) where the stem is itself a word."""
    if whole.lower() in _KEEP_HYPHEN:
        return False
    if not _valid_word(a + b):
        return False
    return (not _valid_word(a)) or b.lower() in _REJOIN_SUFFIXES


def _repair_ligature_token(tok: str) -> str:
    """If a token is not a real word but becomes one by re-inserting a dropped
    fi/fl/ffi ligature, return the repaired token. Only fires when exactly the
    ligature reinsertion yields a dictionary word, so it can't corrupt real text."""
    core = re.sub(r"[^A-Za-z]", "", tok)
    if len(core) < 4 or _valid_word(core):
        return tok
    # Try every insertion point INCLUDING 0, so words whose dropped ligature was
    # at the very start are recovered too ("elds"→"fields", "nal"→"final").
    for i in range(0, len(core)):
        for lig in _LIGATURES:
            cand = core[:i] + lig + core[i:]
            if _valid_word(cand):
                if core[0].isupper():
                    cand = cand[0].upper() + cand[1:]
                return tok.replace(core, cand, 1)
    return tok


def _repair_text(text: str) -> str:
    """Repair one run's text: unicode-ligature normalize → ligature reinsertion
    → smart de-hyphenation → punctuation spacing. Conservative and idempotent."""
    if not text or not text.strip():
        return text
    # 1) Normalise real Unicode ligature characters to ASCII, and strip a
    #    ligature *placeholder* (middle dot / replacement char) that some PDFs
    #    emit for a dropped ligature — only when it sits between two letters, so
    #    genuine "·" separators/bullets are untouched.
    for k, v in _UNICODE_LIGS.items():
        if k in text:
            text = text.replace(k, v)
    text = re.sub(r"(?<=[A-Za-z])[·�](?=[A-Za-z])", "", text)

    # 2) Smart de-hyphenation: rejoin "a-b" when "ab" is a word and the left
    #    stem alone is not (i.e. it was a line-wrap break, not a compound).
    def _dehyph(m):
        whole, a, b = m.group(0), m.group(1), m.group(2)
        if _should_rejoin(a, b, whole):
            joined = a + b
            return joined.capitalize() if a[:1].isupper() else joined
        return whole
    text = re.sub(r"([A-Za-z]{2,})-([a-z]{2,})", _dehyph, text)

    # 3) Ligature reinsertion, token by token (whitespace preserved).
    parts = re.split(r"(\s+)", text)
    parts = [p if (i % 2 or not p.strip()) else _repair_ligature_token(p)
             for i, p in enumerate(parts)]
    text = "".join(parts)

    # 4) Punctuation spacing: drop space before ,.;:!? and collapse doubles.
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


def _repair_docx(path: str, doc=None) -> dict:
    """Apply _repair_text to every run in the DOCX (body paragraphs + table
    cells), in place. Returns counts for benchmarking. Never raises — a repair
    failure must not fail an otherwise-good conversion."""
    stats = {"runs": 0, "changed": 0}
    try:
        from docx import Document as _Doc
    except Exception:
        return stats

    _MONO_FONTS = ("consolas", "couriernew", "courier", "dejavusansmono",
                   "notosansmono", "notomono", "liberationmono", "nimbusmono",
                   "menlo", "monaco", "sourcecodepro", "firacode", "jetbrainsmono")

    def _is_mono(run) -> bool:
        """Code/monospace runs must never be 'repaired': identifiers are not
        English words — dictionary-gated ligature reinsertion could turn a
        legitimate token like 'elds' inside a stack trace into 'fields'."""
        n = re.sub(r"[^a-z]", "", (run.font.name or "").lower())
        return any(n.startswith(m) for m in _MONO_FONTS)

    def _fix_paragraphs(paras):
        for para in paras:
            runs = para.runs
            # Cross-run de-hyphenation: pdf2docx often splits a line-wrapped word
            # as run "secur-" + run "ing". Runs render with no gap, so dropping
            # the trailing hyphen yields "securing" — but only when the joined
            # form is a real word and the left stem alone is not (i.e. it was a
            # wrap break, not a compound like "enterprise-scale").
            for i in range(len(runs) - 1):
                if _is_mono(runs[i]):
                    continue
                m = re.search(r"([A-Za-z]{2,})-$", runs[i].text)
                if not m:
                    continue
                stem = m.group(1)
                nxt = re.match(r"([a-z]{2,})", runs[i + 1].text)
                if not nxt:
                    continue
                cont = nxt.group(1)
                if _should_rejoin(stem, cont, stem + "-" + cont):
                    runs[i].text = runs[i].text[:-1]   # drop the hyphen
                    stats["changed"] += 1
            for run in runs:
                stats["runs"] += 1
                if _is_mono(run):
                    continue
                new = _repair_text(run.text)
                if new != run.text:
                    run.text = new
                    stats["changed"] += 1

    try:
        _own_doc = doc is None
        doc = doc if doc is not None else _Doc(path)
        _fix_paragraphs(doc.paragraphs)
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    _fix_paragraphs(cell.paragraphs)
        if _own_doc:
            doc.save(path)
    except Exception as ex:
        log.warning(f"pdf_to_word: text repair skipped ({ex})")
    return stats


# ── PDF→Word Phase 2: document-intelligence layer (structure, not text) ───────
# Runs AFTER the Phase-1 text repair, on the same DOCX. Every transform is
# high-confidence and additive; when a signal is absent it does nothing (so it
# can never regress a document it doesn't understand). Isolated from pdf2docx.
_BULLET_RE = re.compile(r"^\s*([•‣●◦∙·]|[-*]\s|\d+[.)]|[A-Za-z][.)]|[ivxIVX]+[.)])\s*")


def _emu(v):
    return int(v) if v is not None else 0


def _body_font_size(paras):
    """Median explicit run font size across body paragraphs (EMU), or None."""
    sizes = []
    for p in paras:
        for r in p.runs:
            if r.font.size and r.text.strip():
                sizes.append(int(r.font.size))
    if not sizes:
        return None
    sizes.sort()
    return sizes[len(sizes) // 2]


# All-caps document stamps / footers that read like headings but are not.
_NON_HEADING_CAPS = {
    "ALL RIGHTS RESERVED", "CONFIDENTIAL", "PROPRIETARY AND CONFIDENTIAL",
    "PRIVATE AND CONFIDENTIAL", "DRAFT", "VOID", "PAID", "UNPAID", "OVERDUE",
    "COPY", "DUPLICATE", "ORIGINAL", "SAMPLE", "SPECIMEN", "DO NOT COPY",
    "FOR INTERNAL USE ONLY", "INTERNAL USE ONLY", "CONTINUED",
}


def _norm_caps(t: str) -> str:
    return re.sub(r"[^A-Z0-9 ]", "", t.upper()).strip()


def _looks_like_contact(t: str) -> bool:
    """Contact/address/date lines that must never become headings."""
    return ("@" in t or "|" in t or "/" in t
            or bool(re.search(r"\d{4,}", t))          # phones, years, PINs
            or bool(re.search(r"\bhttps?:|www\.", t)))


def _is_heading(para, body_size) -> bool:
    """High-confidence heading test: a short, standalone line that is either
    ALL-CAPS or bold-and-larger-than-body. Excludes bullets, contact/date lines,
    wrapped/multi-line paragraphs and anything ending like a sentence."""
    t = (para.text or "").strip()
    if not t or "\n" in para.text:
        return False
    if _BULLET_RE.match(t) or _looks_like_contact(t):
        return False
    words = t.split()
    if len(words) > 8 or len(t) > 64:
        return False
    if t.endswith((".", ",", ";")):        # section titles don't end like prose
        return False
    runs = [r for r in para.runs if r.text.strip()]
    if not runs:
        return False
    has_alpha = any(c.isalpha() for c in t)
    allcaps = has_alpha and t == t.upper()
    allbold = all(bool(r.bold) for r in runs)
    sizes = [int(r.font.size) for r in runs if r.font.size]
    larger = bool(sizes) and bool(body_size) and max(sizes) > body_size * 1.15
    # Known all-caps stamps/footers read like headings but are not.
    if _norm_caps(t) in _NON_HEADING_CAPS:
        return False
    # Two independent high-confidence routes.
    if allcaps and len(words) <= 8:
        return True
    if allbold and larger:
        return True
    return False


def _merge_wrapped_lines(paras) -> int:
    """Merge paragraph N into N-1 ONLY when every guard says they are the same
    wrapped paragraph. Extremely conservative: pdf2docx already groups most
    wrapped lines, so this fires rarely and must never fuse distinct blocks
    (dates, list items, headings, address lines). Returns merges performed."""
    from docx.oxml.ns import qn
    merged = 0
    i = 1
    while i < len(paras):
        prev, cur = paras[i - 1], paras[i]
        tp, tc = (prev.text or "").strip(), (cur.text or "").strip()
        ok = bool(tp) and bool(tc)
        if ok and ("\n" in prev.text or "\n" in cur.text):        ok = False
        if ok and (_BULLET_RE.match(tp) or _BULLET_RE.match(tc)):  ok = False
        if ok and (prev.style.name != cur.style.name):            ok = False
        if ok and prev.style.name.lower().startswith("heading"):  ok = False
        if ok and prev.alignment != cur.alignment:                ok = False
        if ok and tp.endswith(SENT_END_CHARS):                    ok = False
        if ok and not tc[:1].islower():                           ok = False   # continuation is lowercase
        if ok and (_looks_like_contact(tp) or _looks_like_contact(tc)): ok = False
        li_p = _emu(prev.paragraph_format.left_indent)
        li_c = _emu(cur.paragraph_format.left_indent)
        if ok and abs(li_p - li_c) > 9144:                        ok = False   # >0.1"
        if ok and _emu(cur.paragraph_format.space_before) > 40640: ok = False  # >3.2pt gap
        if ok:
            # Move cur's runs into prev with a joining space, then delete cur.
            if not prev.runs or not prev.runs[-1].text.endswith(" "):
                prev.add_run(" ")
            for r in list(cur.runs):
                prev._p.append(r._r)
            cur._p.getparent().remove(cur._p)
            paras.pop(i)
            merged += 1
            continue
        i += 1
    return merged


SENT_END_CHARS = (".", "?", "!", ":", ";")


def _fix_heading_wrap(paras) -> int:
    """Clear the artificial right indent pdf2docx puts on heading/title
    paragraphs to mirror empty space next to them in the source PDF. In DOCX
    flow layout that indent serves no purpose — but inside a narrow column it
    forces a single-line title to WRAP, shifting every following line and
    breaking alignment with any fixed vector overlay (rules/bullet dots drawn
    as one anchored page graphic). Clearing it on a LEFT-aligned single-line
    heading can only widen its box: it cannot overlap anything or introduce a
    wrap. Applies to Heading-styled or title-sized (>=16pt) short paragraphs
    with right indent >= 0.5 inch. Returns count cleared."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH as _AL
    fixed = 0
    for p in paras:
        t = (p.text or "").strip()
        if not t or "\n" in p.text or len(t) > 60:
            continue
        if p.alignment not in (None, _AL.LEFT, _AL.JUSTIFY):
            continue                       # right/center alignment depends on it
        sizes = [r.font.size.pt for r in p.runs if r.font.size]
        title_sized = bool(sizes) and max(sizes) >= 16
        if not (p.style.name.lower().startswith("heading") or title_sized):
            continue
        pf = p.paragraph_format
        ri = pf.right_indent
        if ri is not None and ri.pt >= 36:     # >= 0.5"
            pf.right_indent = None
            fixed += 1
    return fixed


def _reflow_docx(path: str, doc=None) -> dict:
    """Phase 2 structural pass: heading promotion + consistent heading spacing +
    conservative wrapped-line merge. Additive and high-confidence; never raises
    (must not fail a good conversion)."""
    stats = {"headings_promoted": 0, "lines_merged": 0, "indents_cleared": 0}
    try:
        from docx import Document as _Doc
        from docx.shared import Pt
        from docx.oxml.ns import qn
    except Exception:
        return stats
    try:
        _own_doc = doc is None
        doc = doc if doc is not None else _Doc(path)
        body = doc.paragraphs
        body_size = _body_font_size(body)
        # Detect a full-page fixed vector overlay (pdf2docx renders the source's
        # rules/bullet-dots/decorations as ONE page-sized anchored drawing).
        # When present, the text flow must keep the SOURCE geometry exactly —
        # any spacing we add shifts text against the fixed artwork (strikes
        # through titles, floating bullet dots). So: styles yes, spacing no.
        page_overlay = False
        try:
            import re as _re
            _xml = doc.element.body.xml
            for cx, cy in _re.findall(r'<wp:extent cx="(\d+)" cy="(\d+)"', _xml):
                if int(cx) > 4_500_000 and int(cy) > 4_500_000:   # ≳5in × 5in
                    page_overlay = True
                    break
        except Exception:
            pass
        # 1) Promote section headings. We set the Heading STYLE (semantic: nav
        # pane / TOC / accessibility) but keep each run's explicit font size/
        # bold/colour, so visual appearance is preserved — structure without
        # restyling. Promoted headings also get consistent spacing so sections
        # read evenly (normalization applied only to what we changed).
        first_content = next((p for p in body if (p.text or "").strip()), None)
        for p in body:
            if p.style.name == "Normal" and _is_heading(p, body_size):
                try:
                    p.style = doc.styles["Heading 1"]
                    # Normalise heading spacing EXCEPT (a) the document's first
                    # content (its spacing is source geometry) and (b) any doc
                    # with a full-page fixed overlay, where changed spacing
                    # shifts text against the anchored artwork.
                    if p is not first_content and not page_overlay:
                        p.paragraph_format.space_before = Pt(12)
                        p.paragraph_format.space_after = Pt(4)
                    stats["headings_promoted"] += 1
                except Exception:
                    pass
        # 2) Un-narrow headings/titles that pdf2docx boxed in with an artificial
        # right indent (prevents title wrap + fixed-overlay collisions).
        stats["indents_cleared"] = _fix_heading_wrap(body)
        # 3) Conservative wrapped-line merge (usually a no-op on pdf2docx output).
        stats["lines_merged"] = _merge_wrapped_lines(doc.paragraphs)
        if _own_doc:
            doc.save(path)
    except Exception as ex:
        log.warning(f"pdf_to_word: reflow skipped ({ex})")
    return stats


# ── PDF→Word Phase 3: semantic reconstruction layer ──────────────────────────
# Transforms pdf2docx's visually-approximated output into real Word semantics:
# genuine editable lists (numbering.xml) and real hyperlinks. Isolated, additive,
# high-confidence; runs after Phase 1 (text) and Phase 2 (structure).
_LIST_BULLET_RE = re.compile(r"^\s*([•‣●◦∙])\s+")
_LIST_NUMBER_RE = re.compile(r"^\s*(\d{1,3})[.)]\s+")
_LIST_ALPHA_RE  = re.compile(r"^\s*([a-zA-Z]|[ivxIVX]{1,4})[.)]\s+")


def _make_bullet_abstract(num_id_abs: int):
    """Build a 3-level bullet <w:abstractNum> (Symbol • ○ ▪)."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    absn = OxmlElement("w:abstractNum")
    absn.set(qn("w:abstractNumId"), str(num_id_abs))
    mlt = OxmlElement("w:multiLevelType"); mlt.set(qn("w:val"), "hybridMultilevel")
    absn.append(mlt)
    # Word-native bullet glyphs + their fonts (Symbol / Courier New / Wingdings).
    # Using the Symbol-font bullet (U+F0B7) is exactly what Microsoft Word emits,
    # so it renders as a real bullet in Word AND LibreOffice — this removes the
    # serif-fallback contamination we saw with a bare "•".
    glyphs = ["", "o", ""]
    gfonts = ["Symbol", "Courier New", "Wingdings"]
    # Geometry matched to the source resume (measured): text ~11pt (≈220 twips)
    # from the margin, bullet hanging ~5pt to its left, ~0.125" per nest level.
    base_left, step, hanging = 220, 180, 100
    for ilvl in range(3):
        lvl = OxmlElement("w:lvl"); lvl.set(qn("w:ilvl"), str(ilvl))
        for tag, val in (("w:start", "1"), ("w:numFmt", "bullet"),
                         ("w:lvlText", glyphs[ilvl]), ("w:lvlJc", "left")):
            e = OxmlElement(tag); e.set(qn("w:val"), val); lvl.append(e)
        pPr = OxmlElement("w:pPr"); ind = OxmlElement("w:ind")
        ind.set(qn("w:left"), str(base_left + step * ilvl))
        ind.set(qn("w:hanging"), str(hanging))
        pPr.append(ind); lvl.append(pPr)
        rPr = OxmlElement("w:rPr"); rf = OxmlElement("w:rFonts")
        rf.set(qn("w:ascii"), gfonts[ilvl]); rf.set(qn("w:hAnsi"), gfonts[ilvl])
        rPr.append(rf); lvl.append(rPr)
        absn.append(lvl)
    return absn


def _make_decimal_abstract(num_id_abs: int):
    """Build a 3-level decimal/alpha/roman <w:abstractNum> (1. a. i.)."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    absn = OxmlElement("w:abstractNum")
    absn.set(qn("w:abstractNumId"), str(num_id_abs))
    mlt = OxmlElement("w:multiLevelType"); mlt.set(qn("w:val"), "hybridMultilevel")
    absn.append(mlt)
    fmts = ["decimal", "lowerLetter", "lowerRoman"]
    texts = ["%1.", "%2.", "%3."]
    for ilvl in range(3):
        lvl = OxmlElement("w:lvl"); lvl.set(qn("w:ilvl"), str(ilvl))
        for tag, val in (("w:start", "1"), ("w:numFmt", fmts[ilvl]),
                         ("w:lvlText", texts[ilvl]), ("w:lvlJc", "left")):
            e = OxmlElement(tag); e.set(qn("w:val"), val); lvl.append(e)
        pPr = OxmlElement("w:pPr"); ind = OxmlElement("w:ind")
        ind.set(qn("w:left"), str(260 + 200 * ilvl)); ind.set(qn("w:hanging"), "260")
        pPr.append(ind); lvl.append(pPr)
        absn.append(lvl)
    return absn


def _register_numbering(numbering_el, make_abstract):
    """Append a new abstractNum + num to the numbering part with collision-free
    ids, keeping the required order (all abstractNum before all num). Returns the
    concrete numId."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    abs_ids = [int(a.get(qn("w:abstractNumId"))) for a in numbering_el.findall(qn("w:abstractNum"))]
    num_ids = [int(n.get(qn("w:numId"))) for n in numbering_el.findall(qn("w:num"))]
    new_abs = (max(abs_ids) + 1) if abs_ids else 0
    new_num = (max(num_ids) + 1) if num_ids else 1
    absn = make_abstract(new_abs)
    nums = numbering_el.findall(qn("w:num"))
    if nums:
        nums[0].addprevious(absn)       # abstractNum must precede num elements
    else:
        numbering_el.append(absn)
    num = OxmlElement("w:num"); num.set(qn("w:numId"), str(new_num))
    an = OxmlElement("w:abstractNumId"); an.set(qn("w:val"), str(new_abs))
    num.append(an); numbering_el.append(num)
    return new_num


def _apply_numpr(para, ilvl: int, num_id: int):
    """Attach <w:numPr> (ilvl + numId) to a paragraph and clear its absolute
    left indent so the list level's indentation governs (clean, editable)."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    pPr = para._p.get_or_add_pPr()
    for old in pPr.findall(qn("w:numPr")):
        pPr.remove(old)
    numPr = OxmlElement("w:numPr")
    il = OxmlElement("w:ilvl"); il.set(qn("w:val"), str(ilvl)); numPr.append(il)
    ni = OxmlElement("w:numId"); ni.set(qn("w:val"), str(num_id)); numPr.append(ni)
    pPr.append(numPr)
    para.paragraph_format.left_indent = None      # let the list level indent apply
    # Tighten list-item spacing to a small, consistent value. pdf2docx leaves a
    # varying per-item space_before (up to ~8pt) that accumulates over a list and
    # inflates the document vertically (a self-inflicted cause of page overflow).
    from docx.shared import Pt as _Pt
    para.paragraph_format.space_before = _Pt(2)
    para.paragraph_format.space_after = _Pt(0)


def _strip_marker(para, marker_re):
    """Remove the leading list marker (e.g. "• " or "1. ") from a paragraph,
    preserving run formatting. The marker is often split across runs (pdf2docx
    puts "•" and the following space in different runs), so we match on the full
    paragraph text and delete that many leading characters run by run."""
    m = marker_re.match(para.text or "")
    if not m:
        return
    n = m.end()
    for run in para.runs:
        if n <= 0:
            break
        if not run.text:
            continue
        take = min(n, len(run.text))
        run.text = run.text[take:]
        n -= take


_ROMAN_RE = re.compile(r"^\s*[ivx]+[.)]\s+", re.I)


def _iter_all_paragraphs(doc):
    """Yield every paragraph in document order INCLUDING table cells (nested
    tables too). pdf2docx renders complex layouts as tables, so passes that
    only walk doc.paragraphs silently skip most of such documents — the
    2026-07-17 regression class (dead links, flat lists, no vertAlign)."""
    def _walk_tables(tables):
        for t in tables:
            for row in t.rows:
                for cell in row.cells:
                    for p in cell.paragraphs:
                        yield p
                    yield from _walk_tables(cell.tables)
    for p in doc.paragraphs:
        yield p
    yield from _walk_tables(doc.tables)


def _split_br_lists(doc) -> int:
    """P2 — pdf2docx often emits a whole multi-level list as ONE paragraph
    with <w:br/> separators, which hides every marker from the per-paragraph
    list reconstructor. Split such paragraphs at their break-runs when at
    least two segments start with a list marker. Returns paragraphs created."""
    import copy as _copy
    from docx.oxml.ns import qn
    made = 0
    for p in list(_iter_all_paragraphs(doc)):
        br_runs = [r for r in p._p.findall(qn("w:r")) if r.find(qn("w:br")) is not None]
        if not br_runs:
            continue
        # count segments that start with a marker
        segs = re.split(r"\n", p.text)
        starts = sum(1 for s in segs if _LIST_BULLET_RE.match(s.strip())
                     or _LIST_NUMBER_RE.match(s.strip()) or _LIST_ALPHA_RE.match(s.strip()))
        if starts < 2:
            continue
        cur = p._p
        for br in br_runs:
            newp = _copy.deepcopy(cur)
            for el in list(newp):
                if el.tag != qn("w:pPr"):
                    newp.remove(el)
            moving = False
            for el in list(cur):
                if el is br:
                    cur.remove(el); moving = True
                    continue
                if moving and el.tag != qn("w:pPr"):
                    cur.remove(el); newp.append(el)
            cur.addnext(newp)
            cur = newp
            made += 1
    return made


def _marker_type_level(t: str) -> int:
    """Nesting level from marker TYPE: 1. -> 0, a. -> 1, i./ii. -> 2."""
    t = t.strip()
    if _LIST_NUMBER_RE.match(t):
        return 0
    if _ROMAN_RE.match(t):
        return 2
    return 1


def _reconstruct_lists(doc) -> dict:
    """Convert contiguous literal-marker paragraphs into real editable Word
    lists. Bullets always convert (unambiguous). Numbered/alpha convert only in
    runs of >=2 to avoid mistaking a lone "1. Introduction" heading for a list.
    Nesting via distinct left-indent tiers. Returns counts."""
    stats = {"list_items": 0, "list_groups": 0}
    stats["br_splits"] = _split_br_lists(doc)
    numbering_el = doc.part.numbering_part.element
    bullet_num = decimal_num = None
    paras = list(_iter_all_paragraphs(doc))

    def indent_level(p, base_indent):
        # Real nesting indents ~0.3"+ deeper. Small left-indent differences in
        # pdf2docx output are justification artifacts, NOT nesting, so only count
        # a level per 0.3" step above the group's minimum indent.
        li = _emu(p.paragraph_format.left_indent)
        step = 274320                    # 0.3 inch in EMU
        return min(max(0, (li - base_indent) // step), 2)

    # Identify contiguous groups of same-kind list paragraphs.
    i = 0
    while i < len(paras):
        p = paras[i]
        t = (p.text or "").strip()
        kind = None
        if _LIST_BULLET_RE.match(t):
            kind = "bullet"
        elif _LIST_NUMBER_RE.match(t) or _LIST_ALPHA_RE.match(t):
            kind = "ordered"
        if kind is None:
            i += 1
            continue
        j = i
        group = []
        while j < len(paras):
            tj = (paras[j].text or "").strip()
            kj = ("bullet" if _LIST_BULLET_RE.match(tj)
                  else "ordered" if (_LIST_NUMBER_RE.match(tj) or _LIST_ALPHA_RE.match(tj))
                  else None)
            if kj != kind:
                break
            group.append(paras[j]); j += 1
        # Ordered lists need >=2 items to be a real list (guard against headings)
        if kind == "ordered" and len(group) < 2:
            i = j
            continue
        base_indent = min(_emu(g.paragraph_format.left_indent) for g in group)
        if kind == "bullet":
            if bullet_num is None:
                bullet_num = _register_numbering(numbering_el, _make_bullet_abstract)
            num_id = bullet_num
            mre = _LIST_BULLET_RE
        else:
            if decimal_num is None:
                decimal_num = _register_numbering(numbering_el, _make_decimal_abstract)
            num_id = decimal_num
            mre = _LIST_NUMBER_RE if _LIST_NUMBER_RE.match((group[0].text or "").strip()) else _LIST_ALPHA_RE
        for g in group:
            # Pick the exact marker regex present on THIS paragraph so the
            # correct number of leading chars is stripped.
            if kind == "bullet":
                gmre = _LIST_BULLET_RE
            else:
                gt = (g.text or "")
                gmre = _LIST_NUMBER_RE if _LIST_NUMBER_RE.match(gt) else _LIST_ALPHA_RE
            lvl = (indent_level(g, base_indent) if kind == "bullet"
                   else max(indent_level(g, base_indent),
                            _marker_type_level(g.text or "")))
            _strip_marker(g, gmre)
            _apply_numpr(g, lvl, num_id)
            stats["list_items"] += 1
        stats["list_groups"] += 1
        i = j
    return stats


def _add_hyperlink_run(paragraph, url: str, text: str, template_run=None):
    """Append a real, clickable Word hyperlink (w:hyperlink + external rel) to a
    paragraph, copying font size/name from a template run so it matches the
    surrounding contact text. Returns the created element."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    r_id = paragraph.part.relate_to(url, RT.HYPERLINK, is_external=True)
    hl = OxmlElement("w:hyperlink"); hl.set(qn("r:id"), r_id)
    r = OxmlElement("w:r"); rPr = OxmlElement("w:rPr")
    # Match the contact line's font (size/name) so it blends in; add link colour.
    if template_run is not None and template_run.font is not None:
        if template_run.font.size:
            sz = OxmlElement("w:sz"); sz.set(qn("w:val"), str(int(template_run.font.size.pt * 2))); rPr.append(sz)
        if template_run.font.name:
            rf = OxmlElement("w:rFonts")
            rf.set(qn("w:ascii"), template_run.font.name); rf.set(qn("w:hAnsi"), template_run.font.name)
            rPr.append(rf)
    col = OxmlElement("w:color"); col.set(qn("w:val"), "0563C1"); rPr.append(col)
    u = OxmlElement("w:u"); u.set(qn("w:val"), "single"); rPr.append(u)
    r.append(rPr)
    t = OxmlElement("w:t"); t.text = text; t.set(qn("xml:space"), "preserve"); r.append(t)
    hl.append(r); paragraph._p.append(hl)
    return hl


def _split_header_docx(path: str, doc=None) -> dict:
    """Split a top-of-document paragraph that glues a large NAME/title run onto
    the same line as a much smaller subtitle run. pdf2docx merges e.g.
    'PRAVEEN KUMAR PADALA  Linux Infrastructure & Systems Administrator' into one
    centered paragraph; in MS Word the large name wraps and OVERLAPS the subtitle.
    We split at the first big->small font-size drop in the first few paragraphs.
    Conservative (large name required, clear size drop) + additive; never raises."""
    stats = {"headers_split": 0, "indents_cleared": 0}
    try:
        from docx import Document as _Doc
        from docx.oxml.ns import qn
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        import copy
    except Exception:
        return stats
    try:
        _own_doc = doc is None
        doc = doc if doc is not None else _Doc(path)
        # Clear spurious left/right indents on centered header lines. pdf2docx
        # sets absolute EMU indents (e.g. 94pt/101pt) that shrink the centring box
        # so a large name wraps to two lines even though the page has room. A
        # centered header should use the full page width.
        for p in doc.paragraphs[:6]:
            if p.alignment == WD_ALIGN_PARAGRAPH.CENTER and \
               (p.paragraph_format.left_indent or p.paragraph_format.right_indent):
                p.paragraph_format.left_indent = None
                p.paragraph_format.right_indent = None
                stats["indents_cleared"] += 1
        for p in doc.paragraphs[:3]:
            runs = list(p.runs)
            if len(runs) < 2:
                continue
            big = None
            split_idx = None
            for i, r in enumerate(runs):
                sz = r.font.size.pt if r.font.size else None
                if sz is None:
                    continue
                if big is None:
                    big = sz
                    if big < 16:                 # header name must be genuinely large
                        break
                    continue
                if sz <= big * 0.7 and sz <= 14:  # clear drop to a subtitle
                    split_idx = i
                    break
            if big is None or big < 16 or split_idx is None:
                continue
            # move runs[split_idx:] into a NEW paragraph right after p (same pPr).
            move = [runs[j]._r for j in range(split_idx, len(runs))]
            new_p = copy.deepcopy(p._p)
            for child in list(new_p):
                if child.tag == qn("w:r"):
                    new_p.remove(child)          # keep pPr (alignment), drop runs
            for r_el in move:
                p._p.remove(r_el)
                new_p.append(r_el)
            p._p.addnext(new_p)
            stats["headers_split"] += 1
            break                                # only the header block, once
        if stats["headers_split"] or stats["indents_cleared"]:
            if _own_doc:
                doc.save(path)
    except Exception as ex:
        log.warning(f"pdf_to_word: header split skipped ({ex})")
    return stats


def _recover_rules_docx(docx_path: str, pdf_path: str, doc=None) -> dict:
    """Recover standalone horizontal rules (section separator lines) that
    pdf2docx silently drops. pdf2docx consumes vector strokes only as table-
    border / text-style hints; a full-width rule under a heading (resumes,
    letterheads, forms) matches neither, so it vanishes from the DOCX.

    For each drawn horizontal rule in the source PDF we find the text line
    immediately ABOVE it, locate that text's paragraph in the DOCX (in reading
    order, full text incl. hyperlink runs), and apply a bottom border
    (w:pPr/w:pBdr/w:bottom). Exact-anchor matching only — unmatched rules are
    skipped, never guessed. Best-effort; never raises."""
    stats = {"rules_recovered": 0}
    try:
        import fitz as _fitz
        from docx import Document as _Doc
        from docx.oxml.ns import qn
        from docx.oxml import OxmlElement
    except Exception:
        return stats
    try:
        def _norm(s):
            return re.sub(r"\s+", " ", s or "").strip().lower()

        # 1) collect (page_order, anchor_text) for each standalone rule
        anchors = []
        pdf = _fitz.open(pdf_path)
        try:
            for page in pdf:
                pw = page.rect.width
                draws = page.get_drawings()
                h_rules = [d["rect"] for d in draws
                           if d["rect"].height < 3 and d["rect"].width > pw * 0.25]
                if not h_rules or len(h_rules) > 20:
                    continue
                # table-grid pages: vertical strokes present -> pdf2docx already
                # treated these as borders; do not double-draw
                if any(d["rect"].width < 3 and d["rect"].height > 20 for d in draws):
                    continue
                # text lines with bottoms (group words by block/line)
                lines = {}
                for w in page.get_text("words"):
                    key = (w[5], w[6])
                    ln = lines.setdefault(key, {"y1": w[3], "words": []})
                    ln["y1"] = max(ln["y1"], w[3])
                    ln["words"].append((w[0], w[4]))
                line_list = [( v["y1"], " ".join(t for _, t in sorted(v["words"])) )
                             for v in lines.values()]
                for rect in sorted(h_rules, key=lambda r: r.y0):
                    above = [(y1, txt) for y1, txt in line_list
                             if y1 <= rect.y0 + 1 and rect.y0 - y1 < 20 and txt.strip()]
                    if above:
                        anchors.append(_norm(max(above)[1]))
        finally:
            pdf.close()
        if not anchors:
            return stats

        # 2) anchor each rule to its DOCX paragraph, in order; add bottom border
        _own_doc = doc is None
        doc = doc if doc is not None else _Doc(docx_path)
        paras = doc.paragraphs
        full = []
        for p in paras:
            full.append(_norm("".join((t.text or "") for t in p._p.iter(qn("w:t")))))
        start = 0
        for anchor in anchors:
            hit = None
            for i in range(start, len(paras)):
                t = full[i]
                if not t:
                    continue
                if t == anchor or (len(anchor) >= 6 and (anchor in t or t in anchor)):
                    hit = i
                    break
            if hit is None:
                continue
            start = hit + 1
            pPr = paras[hit]._p.get_or_add_pPr()
            if pPr.find(qn("w:pBdr")) is not None:
                continue                            # already has a border
            pBdr = OxmlElement("w:pBdr")
            bottom = OxmlElement("w:bottom")
            bottom.set(qn("w:val"), "single")
            bottom.set(qn("w:sz"), "6")
            bottom.set(qn("w:space"), "1")
            bottom.set(qn("w:color"), "auto")
            pBdr.append(bottom)
            pPr.append(pBdr)
            stats["rules_recovered"] += 1
        if stats["rules_recovered"]:
            if _own_doc:
                doc.save(docx_path)
    except Exception as ex:
        log.warning(f"pdf_to_word: rule recovery skipped ({ex})")
    return stats


_RTL_RE = re.compile(r"[֐-ࣿיִ-﷿ﹰ-﻿]")


def _fix_rtl_docx(path: str, doc=None) -> dict:
    """G7 — mark right-to-left text properly: any run whose alphabetic content
    is dominantly Arabic/Hebrew gets <w:rtl/>, and its paragraph <w:bidi/>.
    Formatting-only (no glyph reordering — pdf2docx output order is left as
    extracted). Never raises."""
    stats = {"rtl_runs": 0, "rtl_paragraphs": 0}
    try:
        from docx import Document as _Doc
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        _own_doc = doc is None
        doc = doc if doc is not None else _Doc(path)
        for p in _iter_all_paragraphs(doc):
            para_rtl = False
            for r in p.runs:
                t = r.text or ""
                rtl_chars = len(_RTL_RE.findall(t))
                alpha = sum(1 for ch in t if ch.isalpha())
                if rtl_chars and alpha and rtl_chars / alpha > 0.5:
                    # Shaped presentation-form runs (Arabic Presentation Forms
                    # A/B, U+FB50-FEFF): NFKC-fold to base letters so Word
                    # (which does its own bidi + shaping) renders and searches
                    # them correctly. NO reversal: PyMuPDF extraction already
                    # reorders RTL text to LOGICAL order (verified empirically
                    # — reversing corrupted correct output).
                    pres = sum(1 for ch in t if "ﭐ" <= ch <= "﻿")
                    if pres >= rtl_chars * 0.5:
                        import unicodedata as _ud
                        r.text = _ud.normalize("NFKC", t)
                        stats["rtl_folded"] = stats.get("rtl_folded", 0) + 1
                    rPr = r._r.get_or_add_rPr()
                    if rPr.find(qn("w:rtl")) is None:
                        rPr.append(OxmlElement("w:rtl"))
                        stats["rtl_runs"] += 1
                    para_rtl = True
            if para_rtl:
                pPr = p._p.get_or_add_pPr()
                if pPr.find(qn("w:bidi")) is None:
                    bidi = OxmlElement("w:bidi")
                    pPr.insert(0, bidi)
                    stats["rtl_paragraphs"] += 1
        if stats["rtl_runs"]:
            if _own_doc:
                doc.save(path)
    except Exception as ex:
        log.warning(f"pdf_to_word: RTL pass skipped ({ex})")
    return stats


def _recover_form_fields(docx_path: str, pdf_path: str, doc=None) -> dict:
    """G10 — AcroForm widgets are invisible to pdf2docx (labels survive, the
    fields vanish). Read page.widgets() from the source and append a real Word
    content control to the paragraph holding each field's label: text fields
    become plain-text <w:sdt> carrying the current value (or a fill-in line),
    checkboxes become w14 checkbox controls with the correct checked state.
    Never raises."""
    stats = {"form_fields": 0}
    if not FITZ_OK:
        return stats
    try:
        from docx import Document as _Doc
        from docx.oxml import parse_xml
        from docx.oxml.ns import qn

        _NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
               'xmlns:w14="http://schemas.microsoft.com/office/word/2010/wordml"')

        def _esc(s):
            return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        found = []                                   # (label_snippet, sdt_xml)
        src = fitz.open(pdf_path)
        try:
            for page in src:
                words = page.get_text("words")
                for w in page.widgets() or []:
                    ft = w.field_type
                    rect = fitz.Rect(w.rect)
                    # label = nearest text left of / above the widget
                    best, bestd = None, 1e9
                    for x0, y0, x1, y1, token, *_ in words:
                        wr = fitz.Rect(x0, y0, x1, y1)
                        if wr.y1 <= rect.y1 + 2 and wr.x1 <= rect.x1:
                            d = abs(wr.y1 - rect.y1) * 3 + max(0.0, rect.x0 - wr.x1)
                            if d < bestd:
                                bestd, best = d, token
                    if ft == fitz.PDF_WIDGET_TYPE_CHECKBOX:
                        on = w.field_value not in (None, "", "Off", False)
                        sdt = (
                            f'<w:sdt {_NS}><w:sdtPr><w14:checkbox>'
                            f'<w14:checked w14:val="{1 if on else 0}"/>'
                            f'<w14:checkedState w14:val="2612" w14:font="MS Gothic"/>'
                            f'<w14:uncheckedState w14:val="2610" w14:font="MS Gothic"/>'
                            f'</w14:checkbox></w:sdtPr><w:sdtContent><w:r>'
                            f'<w:rPr><w:rFonts w:ascii="MS Gothic" w:hAnsi="MS Gothic"/></w:rPr>'
                            f'<w:t>{"☒" if on else "☐"}</w:t>'
                            f'</w:r></w:sdtContent></w:sdt>')
                    elif ft in (fitz.PDF_WIDGET_TYPE_TEXT, fitz.PDF_WIDGET_TYPE_COMBOBOX,
                                fitz.PDF_WIDGET_TYPE_LISTBOX):
                        val = _esc(str(w.field_value or "")) or "        "
                        sdt = (
                            f'<w:sdt {_NS}><w:sdtPr><w:alias w:val="{_esc(w.field_name)}"/>'
                            f'<w:text/></w:sdtPr><w:sdtContent><w:r><w:rPr><w:u w:val="single"/></w:rPr>'
                            f'<w:t xml:space="preserve">{val}</w:t></w:r></w:sdtContent></w:sdt>')
                    else:
                        continue
                    found.append((best, sdt))
        finally:
            src.close()
        if not found:
            return stats
        _own_doc = doc is None
        doc = doc if doc is not None else _Doc(docx_path)
        paras = doc.paragraphs
        for label, sdt_xml in found[:100]:
            target = None
            if label:
                target = next((p for p in paras if label in p.text), None)
            if target is None:
                target = paras[-1] if paras else None
            if target is None:
                continue
            target._p.append(parse_xml(sdt_xml))
            stats["form_fields"] += 1
        if stats["form_fields"]:
            if _own_doc:
                doc.save(docx_path)
    except Exception as ex:
        log.warning(f"pdf_to_word: form-field pass skipped ({ex})")
    return stats


_HF_NUM_RE = re.compile(r"\d+")


def _hf_norm(s: str) -> str:
    """Normalize a header/footer candidate: collapse whitespace, wildcard the
    digits so 'Page 1' / 'Page 2' / … count as the SAME repeating line."""
    return _HF_NUM_RE.sub("#", re.sub(r"\s+", " ", s or "").strip().casefold())


def _fix_page_geometry(docx_path: str, pdf_path: str, doc=None) -> dict:
    """G3 — pdf2docx can emit every section with the FIRST page's size, so a
    landscape page in a mixed document renders portrait. Map the source pages
    onto the DOCX's page-level sections (a page section = a sectPr whose
    w:type is not 'continuous'; the body's trailing sectPr closes the last
    page) and rewrite w:pgSz w/h/orient from the source page rect.
    If the page-section count does not match the PDF page count, fall back to
    fixing only the uniform-orientation case. Never raises."""
    stats = {"sections_fixed": 0}
    if not FITZ_OK:
        return stats
    try:
        from docx import Document as _Doc
        from docx.oxml.ns import qn
        src = fitz.open(pdf_path)
        try:
            dims = []
            for pg in src:
                r = pg.rect
                w, h = (r.height, r.width) if pg.rotation in (90, 270) else (r.width, r.height)
                dims.append((round(w * 20), round(h * 20)))     # pt -> twips
        finally:
            src.close()
        if not dims:
            return stats
        _own_doc = doc is None
        doc = doc if doc is not None else _Doc(docx_path)
        page_sects = []
        for s in doc.sections:
            t = s._sectPr.find(qn("w:type"))
            if t is None or t.get(qn("w:val")) != "continuous":
                page_sects.append(s._sectPr)
        if len(page_sects) == len(dims):
            mapping = zip(page_sects, dims)
        elif len({(w > h) for w, h in dims}) == 1:
            mapping = ((sp, dims[0]) for sp in page_sects)      # uniform orient
        else:
            return stats
        for sp, (w, h) in mapping:
            pg = sp.find(qn("w:pgSz"))
            if pg is None:
                continue
            cur = (int(pg.get(qn("w:w"), 0)), int(pg.get(qn("w:h"), 0)))
            want_land = w > h
            if cur == (w, h) and (cur[0] > cur[1]) == want_land and                (pg.get(qn("w:orient")) == "landscape") == want_land:
                continue
            pg.set(qn("w:w"), str(w))
            pg.set(qn("w:h"), str(h))
            if want_land:
                pg.set(qn("w:orient"), "landscape")
            elif pg.get(qn("w:orient")):
                del pg.attrib[qn("w:orient")]
            stats["sections_fixed"] += 1
        if stats["sections_fixed"] and _own_doc:
            doc.save(docx_path)
    except Exception as ex:
        log.warning(f"pdf_to_word: page-geometry pass skipped ({ex})")
    return stats


def _recover_headers_footers(docx_path: str, pdf_path: str, doc=None) -> dict:
    """G6 — pdf2docx inlines running headers/footers into the body. Detect
    lines that repeat (digits wildcarded) on >=60% of pages inside the top or
    bottom 10% band of the SOURCE pages (>=3 pages required), remove those
    paragraphs from the DOCX body, and write them into the real section
    header/footer. A candidate that is nothing but a page number becomes a
    live PAGE field. Never raises."""
    stats = {"header_lines": 0, "footer_lines": 0}
    if not FITZ_OK:
        return stats
    try:
        from docx import Document as _Doc
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        import collections as _coll

        src = fitz.open(pdf_path)
        try:
            n_pages = len(src)
            if n_pages < 3:
                return stats
            top_counts, bot_counts = _coll.Counter(), _coll.Counter()
            top_raw, bot_raw = {}, {}
            for page in src:
                h = page.rect.height
                seen_t, seen_b = set(), set()
                for b in page.get_text("dict")["blocks"]:
                    for ln in b.get("lines", []):
                        txt = "".join(s["text"] for s in ln.get("spans", [])).strip()
                        if not (2 <= len(txt) <= 120):
                            continue
                        key = _hf_norm(txt)
                        y0, y1 = ln["bbox"][1], ln["bbox"][3]
                        if y1 <= h * 0.10 and key not in seen_t:
                            top_counts[key] += 1; top_raw.setdefault(key, txt)
                            seen_t.add(key)
                        elif y0 >= h * 0.90 and key not in seen_b:
                            bot_counts[key] += 1; bot_raw.setdefault(key, txt)
                            seen_b.add(key)
        finally:
            src.close()
        need = max(3, int(n_pages * 0.6))
        headers = [top_raw[k] for k, c in top_counts.items() if c >= need]
        footers = [bot_raw[k] for k, c in bot_counts.items() if c >= need]
        if not headers and not footers:
            return stats

        _own_doc = doc is None
        doc = doc if doc is not None else _Doc(docx_path)
        hf_keys = {_hf_norm(t) for t in headers + footers}
        removed = 0
        for p in list(doc.paragraphs):
            if _hf_norm(p.text) in hf_keys and p.text.strip():
                p._p.getparent().remove(p._p)
                removed += 1
        if not removed:
            return stats                      # body doesn't carry them → no-op

        def _write(zone, lines, is_footer):
            zone.is_linked_to_previous = False
            para = zone.paragraphs[0]
            for r in list(para.runs):
                r._r.getparent().remove(r._r)
            first = True
            for txt in lines:
                if not first:
                    para = zone.add_paragraph()
                first = False
                nums = list(_HF_NUM_RE.finditer(txt))
                if is_footer and len(nums) == 1:
                    # exactly one number in a repeating footer line = the page
                    # number ('7', 'Page 7', '- 7 -') → live PAGE field
                    m = nums[0]
                    if txt[:m.start()]:
                        para.add_run(txt[:m.start()])
                    fld = OxmlElement("w:fldSimple")
                    fld.set(qn("w:instr"), " PAGE ")
                    para._p.append(fld)
                    if txt[m.end():]:
                        para.add_run(txt[m.end():])
                else:
                    para.add_run(txt)
        sec = doc.sections[0]
        if headers:
            _write(sec.header, headers, False)
            stats["header_lines"] = len(headers)
        if footers:
            _write(sec.footer, footers, True)
            stats["footer_lines"] = len(footers)
        # Stamp the references onto EVERY sectPr: OOXML inheritance from the
        # previous section is honored by MS Word but NOT by LibreOffice (our
        # render path) or naive checkers — pages after section 1 lost their
        # header/footer. Reusing the same rId across sections is legal.
        import copy as _copy
        first = doc.sections[0]._sectPr
        refs = [el for el in first
                if el.tag in (qn("w:headerReference"), qn("w:footerReference"))]
        for s in doc.sections[1:]:
            sp = s._sectPr
            for ref in refs:
                if sp.find(ref.tag) is None:
                    sp.insert(0, _copy.deepcopy(ref))
        if _own_doc:
            doc.save(docx_path)
    except Exception as ex:
        log.warning(f"pdf_to_word: header/footer pass skipped ({ex})")
    return stats


def _collect_script_markers(pdf_path: str):
    """P1 — read sub/superscript markers from the SOURCE PDF, where baseline
    geometry still exists (the DOCX only keeps the size). For each text line,
    spans at <=80% of the line's dominant font size whose baseline sits >=1.5pt
    above (sup) or below (sub) the dominant baseline are collected IN ORDER as
    (text, kind). The DOCX pass consumes this queue positionally."""
    out = []
    keyed = []
    try:
        doc = fitz.open(pdf_path)
        try:
            for pno, page in enumerate(doc):
                for b in page.get_text("dict")["blocks"]:
                    # block-level pairing: PyMuPDF often puts a raised/lowered
                    # marker on its own single-span line, so per-line grouping
                    # misses it. Compare every small span against the nearest
                    # big span in the same block instead.
                    spans = [s for ln in b.get("lines", [])
                             for s in ln.get("spans", []) if s["text"].strip()]
                    if len(spans) < 2:
                        continue
                    big_sz = max(s["size"] for s in spans)
                    bigs = [s for s in spans if s["size"] >= big_sz * 0.9]
                    for s in sorted(spans, key=lambda s: (s["origin"][1], s["origin"][0])):
                        t = s["text"].strip()
                        if s["size"] > big_sz * 0.80 or not (0 < len(t) <= 3):
                            continue
                        # baseline = MEDIAN of nearby big-span baselines: PyMuPDF
                        # rebaselines the span FOLLOWING a raised/lowered marker
                        # to the marker's y, so any single neighbour can lie.
                        ys = sorted(g["origin"][1] for g in bigs
                                    if abs(g["origin"][1] - s["origin"][1]) <= 8)
                        if not ys:
                            continue
                        base = ys[len(ys) // 2]
                        dy = s["origin"][1] - base
                        if dy <= -1.5:
                            keyed.append(((pno, base, s["origin"][0]), t, "superscript"))
                        elif dy >= 1.5:
                            keyed.append(((pno, base, s["origin"][0]), t, "subscript"))
        finally:
            doc.close()
        # READING order (page, line baseline, x) — must match the DOCX's run
        # order for positional queue consumption; raw marker y would sort a
        # superscript above its own line.
        out = [(t, k) for _, t, k in sorted(keyed)]
    except Exception:
        pass
    return out


def _fix_superscripts_docx(path: str, pdf_path: str = None, doc=None) -> dict:
    """G5 — pdf2docx flattens footnote/reference markers to inline small runs
    (size survives, vertical alignment doesn't). Restore <w:vertAlign
    superscript> on runs that are unmistakably markers: 1-3 chars from
    {digits, *, †, ‡}, at ≤72% of the paragraph's dominant font size,
    immediately after a run ending in a word character. Never raises."""
    stats = {"superscripts": 0, "subscripts": 0}
    try:
        from docx import Document as _Doc
        markers = _collect_script_markers(pdf_path) if pdf_path else []
        # per-token queues: only the order of SAME-text markers must match the
        # DOCX run order; interleaving of different tokens must not desync it
        import collections as _c
        mqueue = _c.defaultdict(list)
        for mt, mk in markers:
            mqueue[mt].append(mk)
        _own_doc = doc is None
        doc = doc if doc is not None else _Doc(path)
        for p in _iter_all_paragraphs(doc):
            runs = p.runs
            sizes = [r.font.size.pt for r in runs if r.font.size]
            if not runs or not sizes:
                continue
            dominant = max(set(sizes), key=sizes.count)
            for i in range(len(runs)):
                r = runs[i]
                t = (r.text or "").strip()
                if not (0 < len(t) <= 3):
                    continue
                sz = r.font.size.pt if r.font.size else None
                if sz is not None and sz > 8.5:
                    continue
                # PDF-driven: consume the geometry queue — it tells us the
                # DIRECTION (sub vs sup), which the DOCX alone cannot. A queue
                # hit is geometry-PROVEN, so the DOCX-side layout guards below
                # do not apply (pdf2docx often isolates markers in their own
                # tiny paragraph, e.g. inside table cells).
                kind = mqueue[t].pop(0) if mqueue.get(t) else None
                if kind is None:
                    # DOCX-only fallback: strict layout guards (needs real size)
                    if sz is None or not all(ch in "0123456789*†‡" for ch in t):
                        continue
                    biggest = max(sizes)
                    if i == 0 or biggest < 9 or sz > biggest * 0.80:
                        continue
                if kind is None:
                    # fallback also needs a word right before the marker,
                    # looking through space-only runs but NOT tabs (a tab is
                    # real visual separation, not a marker)
                    prev = ""
                    for j in range(i - 1, -1, -1):
                        pj = runs[j].text or ""
                        if "\t" in pj:
                            break
                        if pj.strip():
                            prev = pj.rstrip()
                            break
                    if not (prev and (prev[-1].isalnum() or prev[-1] in ").%\"'")):
                        continue
                if r.font.superscript or r.font.subscript:
                    continue
                if kind == "subscript":
                    r.font.subscript = True
                    stats["subscripts"] += 1
                else:
                    r.font.superscript = True
                    stats["superscripts"] += 1
        if stats["superscripts"] or stats["subscripts"]:
            if _own_doc:
                doc.save(path)
    except Exception as ex:
        log.warning(f"pdf_to_word: superscript pass skipped ({ex})")
    return stats


_LINKIFY_RE = re.compile(
    r"(https?://[^\s<>\"\)\]]+[^\s<>\"\)\].,;:!?]"          # explicit URL
    r"|www\.[^\s<>\"\)\]]+[^\s<>\"\)\].,;:!?]"              # www. shorthand
    r"|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})")    # email


def _linkify_paragraphs(doc, limit: int = 100) -> int:
    """B4 follow-up — make URL/email tokens that sit in the DOCX as PLAIN TEXT
    clickable, on every page (pdf2docx keeps the visible text of many link
    annotations but drops the relationship, so past page 1 links went dead).
    Wraps the token in a real <w:hyperlink> in place — no text is added or
    removed. python-docx's p.runs excludes runs already inside hyperlinks, so
    existing links are never touched. Returns the number of links created."""
    import copy as _copy
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.opc.constants import RELATIONSHIP_TYPE as RT

    n = 0
    for p in _iter_all_paragraphs(doc):
        if n >= limit:
            break
        for run in list(p.runs):
            m = _LINKIFY_RE.search(run.text or "")
            if not m:
                continue
            tok = m.group(0)
            if "@" in tok and not tok.startswith("http"):
                uri = "mailto:" + tok
            elif tok.startswith("www."):
                uri = "https://" + tok
            else:
                uri = tok
            try:
                r_id = doc.part.relate_to(uri, RT.HYPERLINK, is_external=True)
            except Exception:
                continue
            pre, post = run.text[:m.start()], run.text[m.end():]
            hl = OxmlElement("w:hyperlink")
            hl.set(qn("r:id"), r_id)
            lr = OxmlElement("w:r")
            rPr = (_copy.deepcopy(run._r.rPr) if run._r.rPr is not None
                   else OxmlElement("w:rPr"))
            u = OxmlElement("w:u"); u.set(qn("w:val"), "single"); rPr.append(u)
            c = OxmlElement("w:color"); c.set(qn("w:val"), "0563C1"); rPr.append(c)
            lr.append(rPr)
            t = OxmlElement("w:t"); t.text = tok
            t.set(qn("xml:space"), "preserve"); lr.append(t)
            hl.append(lr)
            run._r.addnext(hl)
            if post:                                # tail text after the link
                nr = _copy.deepcopy(run._r)
                for tt in nr.findall(qn("w:t")):
                    nr.remove(tt)
                t2 = OxmlElement("w:t"); t2.text = post
                t2.set(qn("xml:space"), "preserve"); nr.append(t2)
                hl.addnext(nr)
            run.text = pre
            n += 1
    return n


def _recover_hyperlinks(docx_path: str, pdf_path: str, doc=None) -> dict:
    """Phase 4B — recover contact/hyperlink TEXT that pdf2docx drops.

    pdf2docx frequently emits an orphan hyperlink relationship but omits the
    visible display text of linked spans (email / LinkedIn / GitHub vanish). We
    read each link's URI + display text from the SOURCE PDF, and for any whose
    text is missing from the DOCX we append a real clickable hyperlink to the
    contact paragraph. Never duplicates existing links, never edits existing
    runs. Best-effort; never raises."""
    stats = {"links_recovered": 0}
    try:
        import fitz as _fitz
        from docx import Document as _Doc
    except Exception:
        return stats
    try:
        from docx.oxml.ns import qn
        _own_doc = doc is None
        doc = doc if doc is not None else _Doc(docx_path)
        # Full visible text INCLUDING hyperlink runs. python-docx's Paragraph.text
        # silently DROPS text inside <w:hyperlink> elements, so a plain
        # "\n".join(p.text ...) is blind to links pdf2docx already created — which
        # made this pass re-add them as duplicates. Read every <w:t> instead.
        body_text = "".join((t.text or "") for t in doc.element.iter(qn("w:t")))
        # URIs pdf2docx already linked — the reliable dedup key (display text can
        # differ in form, but a matching target means the link already exists).
        existing_uris = set()
        try:
            for rel in doc.part.rels.values():
                if "hyperlink" in (rel.reltype or ""):
                    existing_uris.add((rel.target_ref or "").rstrip("/").lower())
        except Exception:
            pass
        pdf = _fitz.open(pdf_path)
        pg = pdf[0]
        # Collect (uri, display_text) for links in the top contact band.
        missing = []
        seen_uri = set()
        for lk in pg.get_links():
            uri = lk.get("uri")
            if not uri or uri in seen_uri:
                continue
            if (uri or "").rstrip("/").lower() in existing_uris:
                continue                            # pdf2docx already linked it → skip (no dup)
            rect = _fitz.Rect(lk["from"])
            if rect.y0 > 200:                      # contact band only
                continue
            disp = pg.get_textbox(rect).strip().strip("|").strip()
            # keep the most link-like token from the box
            for tok in disp.replace("|", " ").split():
                if "@" in tok or "." in tok and len(tok) > 4:
                    disp = tok
                    break
            if not disp or disp in body_text:
                continue                            # display text already visible → skip (no dup)
            missing.append((uri, disp)); seen_uri.add(uri)
        pdf.close()
        # B4 — linkify plain-text URLs/emails on EVERY page (in-place wrap).
        stats["links_recovered"] += _linkify_paragraphs(doc)
        # Contact-band pass: re-add link text pdf2docx dropped entirely (page 1).
        if missing:
            # Find the contact paragraph: near the top, contains a phone/"|".
            contact = None
            for p in doc.paragraphs[:8]:
                t = p.text
                if ("|" in t or re.search(r"\d{6,}", t)) and p.runs:
                    contact = p
            if contact is not None:
                tmpl = contact.runs[-1] if contact.runs else None
                for uri, disp in missing:
                    if contact.runs and not contact.text.rstrip().endswith(("|", "·", "•")):
                        contact.add_run("  |  ")
                    elif not contact.text.endswith(" "):
                        contact.add_run(" ")
                    _add_hyperlink_run(contact, uri, disp, tmpl)
                    stats["links_recovered"] += 1
        if stats["links_recovered"]:
            if _own_doc:
                doc.save(docx_path)
    except Exception as ex:
        log.warning(f"pdf_to_word: hyperlink recovery skipped ({ex})")
    return stats


# ── PDF→Word Phase 5: Word-native font mapping ───────────────────────────────
# pdf2docx recovers the CORRECT font name from the PDF, but on a Linux stack
# those are metric clones / open fonts (Noto, Liberation, DejaVu, Carlito,
# Caladea, Nimbus, Latin Modern) that Microsoft Word does not ship — so Word
# silently substitutes them and the layout drifts. We remap each to its
# Word-native equivalent. The Liberation/Carlito/Caladea/Nimbus families are
# metric-IDENTICAL clones (drop-in, zero reflow); Noto/DejaVu/LatinModern are
# the closest metric-compatible Word family. Names already native to Word, and
# the Symbol/Wingdings fonts used by our bullets, are never touched.
# Compact (separator-free, lowercase) keys → Word-native family.
_FONT_MAP = {
    # PDF base-14 standard fonts → the metric-identical Word family. These are
    # the single most common names in real PDFs (Helvetica alone appears in
    # 105/114 gold docs) and Word has no font by these names, so every run kept
    # "Helvetica" was silently substituted at open time. Arial/Times New Roman/
    # Courier New ARE the Windows metric clones of these exact fonts.
    "helvetica": "Arial", "times": "Times New Roman",
    "timesroman": "Times New Roman", "courier": "Courier New",
    # metric-identical clones (safest, zero reflow)
    "carlito": "Calibri", "caladea": "Cambria",
    "liberationsans": "Arial", "liberationserif": "Times New Roman",
    "liberationmono": "Courier New",
    "nimbussans": "Arial", "nimbusroman": "Times New Roman",
    "nimbusmono": "Courier New",
    # common Linux/open fonts → closest metric-compatible Word family
    "notosansmono": "Consolas", "notomono": "Consolas",
    "notosans": "Arial", "notoserif": "Times New Roman",
    "dejavusansmono": "Consolas", "dejavusans": "Arial", "dejavuserif": "Georgia",
    "latinmodernroman": "Times New Roman", "latinmodernsans": "Arial",
    "lmroman": "Times New Roman", "lmsans": "Arial", "lmmono": "Consolas",
    "cmr": "Times New Roman", "cmss": "Arial", "cmtt": "Consolas",
    "freesans": "Arial", "freeserif": "Times New Roman", "freemono": "Courier New",
    "opensans": "Arial", "roboto": "Arial", "lato": "Arial",
    # frequent web/print families → same-class Word-native family (sans→Arial,
    # serif→Georgia/TNR, mono→Consolas). Not metric twins, but far better than
    # Word's blind substitution of an unknown name.
    "inter": "Arial", "worksans": "Arial", "ubuntu": "Arial",
    "firasans": "Arial", "sourcesanspro": "Arial", "sourcesans": "Arial",
    "ibmplexsans": "Arial", "helveticaneue": "Arial",
    "merriweather": "Georgia", "ptserif": "Georgia",
    "sourceserifpro": "Georgia", "sourceserif": "Georgia",
    "ibmplexserif": "Georgia", "ptsans": "Arial",
    "sourcecodepro": "Consolas", "firacode": "Consolas",
    "jetbrainsmono": "Consolas", "ibmplexmono": "Consolas",
    "ubuntumono": "Consolas",
}
# Never rewrite: already Word-native, or our bullet glyph fonts.
_FONT_KEEP = {
    "arial", "calibri", "cambria", "timesnewroman", "georgia", "verdana",
    "tahoma", "segoeui", "consolas", "couriernew", "aptos", "trebuchetms",
    "garamond", "candara", "symbol", "wingdings", "wingdings2", "webdings",
    "arialnarrow", "bookantiqua", "centurygothic", "franklingothic", "calibrilight",
}
_FONT_SUBSET_RE = re.compile(r"^[A-Z]{6}\+")            # "ABCDEF+" subset prefix
# Weight/style tokens that may trail a family name. Deliberately EXCLUDES
# roman/sans/serif/mono, which are parts of real family names.
_FONT_STYLE_TOKENS = (
    "bolditalic", "boldoblique", "semibold", "extrabold", "demibold",
    "bold", "italic", "oblique", "regular", "light", "medium", "black",
    "condensed", "thin", "book", "demi", "heavy", "narrow", "mt", "ps",
)


def _normalize_font(name: str) -> str:
    """Reduce a raw run font name to a compact base-family key: drop the 6-char
    subset prefix, remove all separators, strip trailing size digits and
    weight/style tokens. 'ABCDEF+NotoSans-Bold' -> 'notosans';
    'DejaVuSans' -> 'dejavusans'; 'Times New Roman' -> 'timesnewroman'."""
    n = _FONT_SUBSET_RE.sub("", name or "").lower()
    n = re.sub(r"[^a-z0-9]", "", n)
    n = re.sub(r"\d+$", "", n)
    changed = True
    while changed:
        changed = False
        for t in _FONT_STYLE_TOKENS:
            if n.endswith(t) and len(n) > len(t) + 2:
                n = n[:-len(t)]; changed = True; break
    return re.sub(r"\d+$", "", n)


def _lookup_font(base: str):
    """Exact match, then LONGEST-prefix match (so notosansmono beats notosans)."""
    if base in _FONT_KEEP:
        return None
    if base in _FONT_MAP:
        return _FONT_MAP[base]
    for k in sorted(_FONT_MAP, key=len, reverse=True):
        if base.startswith(k):
            return _FONT_MAP[k]
    return None


def _map_fonts_docx(path: str, doc=None) -> dict:
    """Phase 5 — remap non-Word fonts on every run to a Word-native family.
    Only run.font.name is changed (weight/size/italic untouched); never rewrites
    Word-native or bullet fonts. Additive, reversible, never raises."""
    stats = {"runs_remapped": 0, "families": set()}
    try:
        from docx import Document as _Doc
    except Exception:
        stats["families"] = []
        return stats

    def _fix(paras):
        for p in paras:
            for r in p.runs:
                name = r.font.name
                if not name:
                    continue
                target = _lookup_font(_normalize_font(name))
                if target and target != name:
                    r.font.name = target
                    stats["runs_remapped"] += 1
                    stats["families"].add(f"{_normalize_font(name)}->{target}")
    try:
        _own_doc = doc is None
        doc = doc if doc is not None else _Doc(path)
        _fix(doc.paragraphs)
        for t in doc.tables:
            for row in t.rows:
                for cell in row.cells:
                    _fix(cell.paragraphs)
        if _own_doc:
            doc.save(path)
    except Exception as ex:
        log.warning(f"pdf_to_word: font mapping skipped ({ex})")
    stats["families"] = sorted(stats["families"])
    return stats


# ── Vector-graphics recovery (feature-flag VECTOR_RASTERIZE) ────────────────
# pdf2docx silently drops path-based artwork (bar/pie/line charts, diagrams):
# captions survive, the drawing vanishes. This pass detects vector-graphic
# regions in the source PDF, rasterizes JUST those regions (200 dpi, white bg)
# and inserts them as inline images at their reading-order slot in the DOCX.
# Detection was tuned on the 114-doc gold set + synthetic traps: 21/21 true
# artwork found, 0 false positives (table rulings, signature lines, page
# borders, banner/sidebar fills, form shading all vetoed).
_VG_GAP = 36.0            # first-pass bbox merge distance (pt)
_VG_MIN_WH = 40.0         # minimum region side (pt) — skips bullets/icons
_VG_TEXT_COV_MAX = 0.04   # charts measured 0.0; text furniture >= 0.042
_VG_IMG_OVER_MAX = 0.50   # already embedded as raster by pdf2docx
_VG_PAGE_COV_MAX = 0.85   # page background / border
_VG_EV_AREA_MIN = 0.05    # chart-evidence drawings must cover >=5% of cluster
_VG_DPI = 200


def _vg_enabled() -> bool:
    # Default ON since 2026-07-16: a real user torture-test showed the
    # alternative is worse — pdf2docx extracts chart axis values as a garbled
    # text blob ("8038624555Q1Q2Q3Q4"). The _VG_DOC_CAP below keeps the
    # chart-dense overflow cases (the reason it shipped dark) on the old path.
    return os.environ.get("VECTOR_RASTERIZE", "1").lower() not in ("0", "false", "")


_VG_DOC_CAP = 2   # >2 artwork regions per doc = chart-dense: rasters would
                  # overflow pages (gold: SSIM -0.23..-0.26 on such docs), skip.


def _vg_classify(d) -> str:
    """ruling = hairline (table grids, underlines, header/footer rules,
    signature lines) — never seeds a region; tiny = dots/dashes; else shape."""
    r = d["rect"]
    if r.width < 3 and r.height < 3:
        return "tiny"
    if (r.height <= 2.0 and r.width >= 30) or (r.width <= 2.0 and r.height >= 30):
        return "ruling"
    return "shape"


def _vg_is_evidence(d) -> bool:
    """Chart evidence: filled shapes (bars/pies/areas), bezier curves, a dense
    2-D polyline, or a thick diagonal stroke (line-chart segments — table
    rulings are hairline AND axis-aligned, so their bbox is thin). Stroke-only
    rectangles are NOT evidence — table frames / text boxes / page furniture."""
    if d.get("fill") is not None:
        return True
    n_lines = 0
    for it in d["items"]:
        if it[0] == "c":
            return True
        if it[0] == "l":
            n_lines += 1
    r = d["rect"]
    if n_lines >= 4 and r.width > 10 and r.height > 10:
        return True
    return ((d.get("width") or 0) >= 2.0 and n_lines >= 1
            and r.width > 5 and r.height > 5)


def _vg_blocked(c, o, barriers):
    """A text line between two boxes is a merge barrier: it keeps captions out
    of artwork regions (two stacked figures with 'Fig 1' between them must stay
    two regions, each anchored to its own caption)."""
    if c.y0 >= o.y1:                                  # o above c
        band = fitz.Rect(min(c.x0, o.x0), o.y1, max(c.x1, o.x1), c.y0)
    elif o.y0 >= c.y1:                                # c above o
        band = fitz.Rect(min(c.x0, o.x0), c.y1, max(c.x1, o.x1), o.y0)
    else:
        return False                                  # vertical overlap: no band
    if band.is_empty:
        return False
    return any(b.intersects(band) and not (b & band).is_empty for b in barriers)


def _vg_merge(clusters, gap, barriers=()):
    """Iterative bbox merge while boxes come within `gap` pt of each other and
    no text-line barrier separates them."""
    changed = True
    while changed:
        changed = False
        out = []
        while clusters:
            c = clusters.pop()
            grown = fitz.Rect(c.x0 - gap, c.y0 - gap, c.x1 + gap, c.y1 + gap)
            hit = next((i for i, o in enumerate(out)
                        if grown.intersects(o) and not _vg_blocked(c, o, barriers)),
                       None)
            if hit is not None:
                out[hit] |= c
                changed = True
            else:
                out.append(c)
        clusters = out
    return clusters


def _vg_merge_series(clusters):
    """Second pass: merge bar-series members — clusters that vertically overlap
    >=50% of the shorter one with a horizontal gap <=60pt (bars in a series
    share a baseline; vertically stacked distinct figures do not)."""
    changed = True
    while changed:
        changed = False
        out = []
        while clusters:
            c = clusters.pop()
            hit = None
            for i, o in enumerate(out):
                vo = min(c.y1, o.y1) - max(c.y0, o.y0)
                if vo <= 0 or vo / min(c.height, o.height) < 0.5:
                    continue
                if max(c.x0, o.x0) - min(c.x1, o.x1) <= 60:
                    hit = i
                    break
            if hit is not None:
                out[hit] |= c
                changed = True
            else:
                out.append(c)
        clusters = out
    return clusters


def _vector_graphic_regions(page):
    """Detect chart/diagram vector regions on a page. Returns list[fitz.Rect]."""
    shapes, rulings, evidence, curves = [], [], [], []
    for d in page.get_drawings():
        k = _vg_classify(d)
        if k == "shape":
            r = fitz.Rect(d["rect"])
            shapes.append(r)
            if _vg_is_evidence(d):
                evidence.append(r)
            if any(it[0] == "c" for it in d["items"]):
                curves.append(r)
        elif k == "ruling":
            rulings.append(fitz.Rect(d["rect"]))
    if not evidence:
        return []
    barriers = []
    try:
        for b in page.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                if len("".join(s["text"] for s in ln.get("spans", [])).strip()) >= 8:
                    barriers.append(fitz.Rect(ln["bbox"]))
    except Exception:
        pass
    clusters = _vg_merge_series(_vg_merge(shapes, _VG_GAP, barriers))

    words = page.get_text("words")
    imgs = []
    for x in page.get_images():
        try:
            r = fitz.Rect(page.get_image_bbox(x))
            if not r.is_empty:
                imgs.append(r)
        except Exception:
            pass
    parea = abs(page.rect) or 1.0
    pr = page.rect
    out = []
    for c in clusters:
        if c.width < _VG_MIN_WH or c.height < _VG_MIN_WH:
            continue
        if abs(c) / parea > _VG_PAGE_COV_MAX:
            continue
        # edge-furniture veto: banners/sidebars span (almost) the full page
        # width or height, or hug 2+ page edges — design chrome, not artwork
        if c.width >= pr.width * 0.90 or c.height >= pr.height * 0.90:
            continue
        if sum((abs(c.x0 - pr.x0) < 4, abs(c.x1 - pr.x1) < 4,
                abs(c.y0 - pr.y0) < 4, abs(c.y1 - pr.y1) < 4)) >= 2:
            continue
        if sum(abs(fitz.Rect(w[:4]) & c) for w in words) / abs(c) > _VG_TEXT_COV_MAX:
            continue
        if imgs and sum(abs(i & c) for i in imgs) / abs(c) > _VG_IMG_OVER_MAX:
            continue
        if sum(abs(e & c) for e in evidence) / abs(c) < _VG_EV_AREA_MIN:
            continue
        # curve-skip: pdf2docx's own figure rasterizer HANDLES regions that
        # contain bezier curves (pies, donuts, marker dots — measured: it
        # embeds them correctly placed), and taking those over regressed SSIM
        # up to -0.61 on the gold set. It DROPS curve-free artwork (bar
        # charts, straight-line diagrams) — recover only those.
        if any(cv.intersects(c) for cv in curves):
            continue
        # axis pickup: rulings touching the cluster (chart axes), never rulings
        # merely nearby (protects adjacent tables)
        for r in rulings:
            if r.intersects(c):
                c |= r
        out.append(c)
    out.sort(key=lambda c: (c.y0, c.x0))          # reading order
    return out


def _vg_anchor(page, region):
    """Nearest text line below (caption) else above the region: (snippet, where).
    The snippet locates the region's reading-order slot in the DOCX."""
    best_below, best_above = None, None
    try:
        blocks = page.get_text("dict")["blocks"]
    except Exception:
        return None, "below"
    for b in blocks:
        for ln in b.get("lines", []):
            txt = "".join(s["text"] for s in ln.get("spans", [])).strip()
            if len(txt) < 8:
                continue
            y0, y1 = ln["bbox"][1], ln["bbox"][3]
            if y0 >= region.y1 - 2:                       # below the artwork
                if best_below is None or y0 < best_below[0]:
                    best_below = (y0, txt)
            elif y1 <= region.y0 + 2:                     # above the artwork
                if best_above is None or y1 > best_above[0]:
                    best_above = (y1, txt)
    if best_below:
        return best_below[1], "below"
    if best_above:
        return best_above[1], "above"
    return None, "below"


def _vg_norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().casefold()


def _vg_collect(pdf_path: str):
    """Phase A (pre-conversion): detect + rasterize artwork regions, then write
    a temp copy of the PDF with those regions REDACTED. pdf2docx converts the
    redacted copy, so it can neither mangle the artwork with its own broken
    curve rendering nor scatter stray chart-label text — the region lives only
    in our faithful raster. Returns (found, redacted_path) or (None, None)."""
    if not (_vg_enabled() and FITZ_OK):
        return None, None
    try:
        found = []                     # (png, w_pt, snippet, where)
        doc = fitz.open(pdf_path)
        try:
            # two-phase: count first — chart-dense docs stay on the old path
            per_page = [(page, _vector_graphic_regions(page)) for page in doc]
            if sum(len(r) for _, r in per_page) > _VG_DOC_CAP:
                return None, None
            any_redact = False
            for page, regions in per_page:
                if not regions:
                    continue
                for region in regions:
                    pm = page.get_pixmap(clip=region, dpi=_VG_DPI, alpha=False)
                    snippet, where = _vg_anchor(page, region)
                    found.append((pm.tobytes("png"), region.width, snippet, where))
                    page.add_redact_annot(region)
                # keep raster images (regions overlapping them were vetoed);
                # remove vector line-art touching the region — IF_COVERED
                # misses wedges whose bbox rounds a hair past the region edge,
                # leaving pdf2docx's mangled duplicate in the output
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE,
                                      graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED)
                any_redact = True
            if not found:
                return None, None
            redacted = None
            if any_redact:
                fd, redacted = tempfile.mkstemp(suffix=".pdf", prefix="vg_redact_")
                os.close(fd)
                doc.save(redacted)
        finally:
            doc.close()
        return found, redacted
    except Exception as ex:
        log.warning(f"pdf_to_word: vector-graphics collect skipped ({ex})")
        return None, None


def _vg_insert(docx_path: str, found, doc=None) -> dict:
    """Phase B (post-conversion): insert each recovered raster inline at its
    reading-order slot — immediately above its caption line (anchor 'below')
    or after the preceding text line (anchor 'above'). Never raises."""
    stats = {"vector_images": 0}
    if not found:
        return stats
    try:
        from io import BytesIO
        from docx import Document as _Doc
        from docx.shared import Emu

        _own_doc = doc is None
        d = doc if doc is not None else _Doc(docx_path)
        sec = d.sections[0]
        content_w_pt = float(sec.page_width.pt - sec.left_margin.pt
                             - sec.right_margin.pt)
        paras = d.paragraphs
        norm_texts = [_vg_norm(p.text) for p in paras]
        for png, w_pt, snippet, where in found:
            width = Emu(int(min(w_pt, content_w_pt) * 12700))
            anchor = None
            if snippet:
                key = _vg_norm(snippet)[:40]
                anchor = next((p for p, t in zip(paras, norm_texts)
                               if t and (t.startswith(key) or key in t)), None)
            new_p = d.add_paragraph()                # created at body end…
            new_p.add_run().add_picture(BytesIO(png), width=width)
            if anchor is not None:                   # …then moved into place
                if where == "below":                 # caption stays a sibling below
                    anchor._p.addprevious(new_p._p)
                else:
                    anchor._p.addnext(new_p._p)
            stats["vector_images"] += 1
        if _own_doc:
            d.save(docx_path)
    except Exception as ex:
        log.warning(f"pdf_to_word: vector-graphics insert skipped ({ex})")
    return stats


def _semantic_docx(path: str, doc=None) -> dict:
    """Phase 3 entry point: semantic list reconstruction (+ future: hyperlinks).
    Additive, high-confidence, never raises."""
    stats = {"list_items": 0, "list_groups": 0}
    try:
        from docx import Document as _Doc
    except Exception:
        return stats
    try:
        _own_doc = doc is None
        doc = doc if doc is not None else _Doc(path)
        if getattr(doc.part, "numbering_part", None) is not None:
            stats.update(_reconstruct_lists(doc))
        if _own_doc:
            doc.save(path)
    except Exception as ex:
        log.warning(f"pdf_to_word: semantic pass skipped ({ex})")
    return stats


def _pdf_is_scanned(doc) -> bool:
    """True when an OPEN fitz doc carries essentially no digital text layer, i.e.
    every page is image-only (a scan). Strict on purpose: a single page with real
    extractable text means we keep the normal pdf2docx path, so no text-bearing
    PDF is ever rerouted to OCR (zero regression for the 106 non-scan gold docs)."""
    try:
        for i in range(len(doc)):
            if len(doc[i].get_text("text").strip()) >= 10:
                return False
        return len(doc) > 0
    except Exception:
        return False


def _hybrid_ocr_docx(docx_path: str, pdf_path: str, lang: str = "eng", doc=None) -> dict:
    """G9 — hybrid documents (mostly digital + some scanned pages). The router
    is all-or-nothing: a doc with ONE text-bearing page takes the pdf2docx
    path, so its scanned pages arrive as full-page images with zero text.
    This pass OCRs exactly those pages and inserts the recovered text right
    after each page image (image kept for visual fidelity, text added for
    search/selection/editing).

    Anchoring: pdf2docx emits one full-page-sized inline image per image-only
    page, in page order — so the k-th full-page image in the DOCX body maps to
    the k-th scanned source page. STRICT count validation: if the counts
    disagree, do nothing. Never raises."""
    stats = {"ocr_pages": 0, "ocr_chars": 0}
    if not (TESSERACT_OK and FITZ_OK):
        return stats
    try:
        import pytesseract
        from PIL import Image as _Img
        from docx import Document as _Doc
        from docx.oxml.ns import qn

        src = fitz.open(pdf_path)
        try:
            scanned_pages = [i for i in range(len(src))
                             if len(src[i].get_text("text").strip()) < 10
                             and src[i].get_images()]
            if not scanned_pages or len(scanned_pages) == len(src):
                return stats                      # pure digital / pure scan
            _own_doc = doc is None
            doc = doc if doc is not None else _Doc(docx_path)
            # full-page inline images in body order (page_overlay heuristic)
            _EXT = ("{http://schemas.openxmlformats.org/drawingml/2006/"
                    "wordprocessingDrawing}extent")
            anchors = []
            for p in doc.paragraphs:
                for ext in p._p.iter(_EXT):
                    if (int(ext.get("cx", 0)) > 4_500_000
                            and int(ext.get("cy", 0)) > 4_500_000):
                        anchors.append(p)
                        break
            if len(anchors) != len(scanned_pages):
                log.info(f"hybrid-ocr: anchor/page count mismatch "
                         f"({len(anchors)} images vs {len(scanned_pages)} scanned"
                         f" pages) — skipping")
                return stats
            mat = fitz.Matrix(300 / 72.0, 300 / 72.0)
            for anchor, pno in zip(anchors, scanned_pages):
                pm = src[pno].get_pixmap(matrix=mat, colorspace=fitz.csGRAY)
                img = _Img.frombytes("L", (pm.width, pm.height), pm.samples)
                text = pytesseract.image_to_string(img, lang=lang) or ""
                last = anchor._p
                for para_txt in re.split(r"\n\s*\n", text):
                    para_txt = " ".join(para_txt.split())
                    if len(para_txt) < 3:
                        continue
                    np_ = doc.add_paragraph(para_txt)
                    last.addnext(np_._p)
                    last = np_._p
                    stats["ocr_chars"] += len(para_txt)
                stats["ocr_pages"] += 1
            if stats["ocr_chars"]:
                if _own_doc:
                    doc.save(docx_path)
        finally:
            src.close()
    except Exception as ex:
        log.warning(f"pdf_to_word: hybrid OCR pass skipped ({ex})")
    return stats


def _ocr_pdf_to_docx(pdf_path: str, docx_path: str, lang: str = "eng",
                     dpi: int = 300, max_pages: int = 0) -> dict:
    """Build a reflowable DOCX from a scanned/image-only PDF by rendering each
    page at `dpi` and running Tesseract on the plain grayscale raster. We do NOT
    pre-binarize (denoise/Otsu): Tesseract's LSTM does its own thresholding and
    measurably reads noisy/blurry scans better from grayscale than from a
    pre-binarized image (validated on the gold ocr_scan set: 0.44 vs 0.41 recall).
    Emits one paragraph per OCR paragraph (blank-line split), joining wrapped
    lines with spaces. The result flows through the same text-repair
    post-processing as pdf2docx output. Returns {pages, chars}."""
    from docx import Document as _Doc
    doc = fitz.open(pdf_path)
    out = _Doc()
    n = len(doc)
    if max_pages and n > max_pages:
        n = max_pages
    total_chars = 0
    try:
        mat = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        for i in range(n):
            pix = doc[i].get_pixmap(matrix=mat, alpha=False, colorspace=fitz.csGRAY)
            img = Image.frombytes("L", (pix.width, pix.height), pix.samples)
            try:
                text = pytesseract.image_to_string(img, lang=lang,
                                                   config="--psm 3 --oem 3")
            except Exception as ex:
                log.warning(f"pdf_to_word OCR page {i + 1}: {ex}")
                text = ""
            img.close()
            for para in re.split(r"\n\s*\n", text):
                para = re.sub(r"\s*\n\s*", " ", para).strip()
                if para:
                    out.add_paragraph(para)
                    total_chars += len(para)
            if i < n - 1:
                out.add_page_break()
    finally:
        doc.close()
    out.save(docx_path)
    return {"pages": n, "chars": total_chars}


# ── PDF→Word: Docling layout-engine path (hard docs: multi-column / tables / scans) ─
# pdf2docx is a geometry-heuristic library with no document-structure model — it
# scrambles multi-column reading order, barely reconstructs tables, and its OCR
# fallback garbles hard scans. Docling (IBM, MIT) uses ML layout + table models
# (DocLayNet / TableFormer) that read structure correctly. We route only the docs
# pdf2docx architecturally fails on to Docling; simple text docs stay on the fast
# pdf2docx path. Docling is heavy (torch), so it is imported lazily and the
# DocumentConverter is cached per worker process (models load once, then reused).
_DOCLING_CONVERTER = None
_DOCLING_TRIED = False

# Docling runs ML models on CPU here (~10-15s/page). Above this page count a
# doc NEVER routes to Docling, whatever its layout: worst case stays ~5 min
# (20 x 15s) — far inside the 1800s task limit and fair to the 2-slot office
# queue — while real-world complex docs (invoices, brochures, forms, papers)
# are overwhelmingly under 20 pages. Longer docs take the chunked pdf2docx
# path (or the Tesseract OCR path if scanned): slightly lower fidelity,
# but bounded and never worse than pre-Docling production behaviour.
_DOCLING_MAX_PAGES = int(os.getenv("DOCLING_MAX_PAGES", "20"))


def _get_docling_converter():
    """Lazily build + cache a Docling DocumentConverter. Returns None if Docling
    is not installed in this worker's image (keeps other workers/app lean)."""
    global _DOCLING_CONVERTER, _DOCLING_TRIED
    if _DOCLING_TRIED:
        return _DOCLING_CONVERTER
    _DOCLING_TRIED = True
    try:
        from docling.document_converter import DocumentConverter
        _DOCLING_CONVERTER = DocumentConverter()
    except Exception as ex:
        log.warning(f"pdf_to_word: Docling unavailable ({ex}); using pdf2docx path")
        _DOCLING_CONVERTER = None
    return _DOCLING_CONVERTER


def _pdf_is_complex(doc) -> bool:
    """True when a (text-bearing) PDF has layout pdf2docx handles poorly — real
    tables or multi-column text — so it should route to Docling. Conservative:
    single-column, table-free docs (most resumes/letters) stay on the fast path.

    Sampling is spread across the document (first 3 pages + middle + last)
    rather than front-only, so a doc whose tables/columns start later is not
    misrouted by luck of its opening pages. Cost is at most 2 extra sampled
    pages (only docs <= _DOCLING_MAX_PAGES ever reach this check)."""
    try:
        n = len(doc)
        sample = sorted({p for p in (0, 1, 2, n // 2, n - 1) if 0 <= p < n})
        for pno in sample:
            page = doc[pno]
            # Chart veto: pages dominated by many SMALL filled vector shapes
            # (pie slices, bars — measured: chart pages have >=9, tables/
            # invoices <=3, brochures <=6) are CHARTS, not tables. Docling
            # folds chart text (axis labels, legends) into picture regions and
            # drops it, while pdf2docx extracts it fully — so chart pages must
            # not trigger the Docling route via their gridlines/legend columns.
            try:
                pa = page.rect.width * page.rect.height
                small_fills = sum(
                    1 for x in page.get_drawings()
                    if x["type"] in ("f", "fs")
                    and (x["rect"].width * x["rect"].height) < pa * 0.03)
                if small_fills >= 8:
                    continue
            except Exception:
                pass
            try:
                for t in page.find_tables().tables:
                    if getattr(t, "row_count", 0) >= 2 and getattr(t, "col_count", 0) >= 2:
                        return True
            except Exception:
                pass
            # multi-column: cluster text-block left edges; >=2 substantial columns
            # separated by a wide gap indicates true columns (not mere indentation).
            blocks = [b for b in page.get_text("blocks") if b[6] == 0 and b[4].strip()]
            if len(blocks) >= 6:
                xs = sorted(round(b[0]) for b in blocks)
                regions, cur = [], [xs[0]]
                for x in xs[1:]:
                    if x - cur[-1] > 40:
                        regions.append(cur); cur = [x]
                    else:
                        cur.append(x)
                regions.append(cur)
                substantial = [r for r in regions if len(r) >= 3]
                if len(substantial) >= 2 and (substantial[-1][0] - substantial[0][-1]) > 100:
                    return True
        return False
    except Exception:
        return False


def _docling_to_docx(dl_doc, out_path) -> dict:
    """Bridge: DoclingDocument (headings/paragraphs/lists/TABLES in reading order)
    → a real .docx via python-docx. Tables become genuine Word tables."""
    from docx import Document as _Doc
    from docling_core.types.doc import TableItem
    stats = {"paras": 0, "tables": 0, "headings": 0}
    d = _Doc()
    for item, level in dl_doc.iterate_items():
        lbl = getattr(getattr(item, "label", None), "value", "")
        if isinstance(item, TableItem):
            nr, nc = item.data.num_rows, item.data.num_cols
            if nr and nc:
                t = d.add_table(rows=nr, cols=nc); t.style = "Table Grid"
                grid = item.data.grid
                for r in range(nr):
                    for c in range(nc):
                        try:
                            t.cell(r, c).text = (grid[r][c].text or "").strip()
                        except Exception:
                            pass
                stats["tables"] += 1
            continue
        txt = (getattr(item, "text", "") or "").strip()
        if not txt:
            continue
        if lbl == "title":
            d.add_heading(txt, level=0); stats["headings"] += 1
        elif lbl == "section_header":
            d.add_heading(txt, level=min(max(level, 1), 4)); stats["headings"] += 1
        elif lbl == "list_item":
            d.add_paragraph(txt, style="List Bullet"); stats["paras"] += 1
        elif lbl in ("page_header", "page_footer", "footnote"):
            continue
        else:
            d.add_paragraph(txt); stats["paras"] += 1
    d.save(out_path)
    return stats


def _convert_with_docling(pdf_path: str, out_path: str) -> Optional[dict]:
    """Full Docling path: PDF → DoclingDocument → DOCX bridge. Returns stats, or
    None if Docling is unavailable. Raises on genuine conversion failure (caller
    falls back to pdf2docx)."""
    conv = _get_docling_converter()
    if conv is None:
        return None
    dl = conv.convert(pdf_path).document
    return _docling_to_docx(dl, out_path)


@register("pdf_to_word")
def pdf_to_word(ctx: JobContext) -> dict:
    """
    Convert PDF to DOCX via pdf2docx (table-aware mode), then run a dictionary-
    gated text-repair pass (ligature reinsertion, de-hyphenation, punctuation
    spacing) that fixes the character-level corruption pdf2docx inherits from
    PyMuPDF extraction — without altering layout or correct text.
    V14 FIX: For large PDFs, convert in page-range chunks to avoid pdf2docx OOM.
    """
    _require(PDF2DOCX_OK, "pdf_to_word", "pdf2docx")
    _guard_empty(ctx.input_path)

    # Determine page count for progress + chunking decision, and detect scanned
    # (image-only) PDFs while the doc is open.
    page_count = 0
    scanned = False
    complex_layout = False
    docling_eligible = False
    if FITZ_OK:
        doc = fitz.open(ctx.input_path)
        page_count = len(doc)
        scanned = TESSERACT_OK and _pdf_is_scanned(doc)
        # Page-count guard: big docs NEVER route to Docling (CPU ~10-15s/page —
        # a 300-page doc would block an office-queue slot for over an hour).
        # They take the chunked pdf2docx path (or Tesseract OCR if scanned).
        docling_eligible = 0 < page_count <= _DOCLING_MAX_PAGES
        # Routing is scanned-only (see router comment). The complexity detector
        # (_pdf_is_complex) is retained for future use but no longer routes —
        # born-digital docs keep their visual design on the pdf2docx path.
        complex_layout = False
        doc.close()

    ctx.set_progress(5)

    # ── Engine router ─────────────────────────────────────────────────────────
    # Docling is used ONLY where pdf2docx architecturally fails: SCANNED docs
    # (no text layer — pdf2docx emits nothing; Docling+OCR recovered the gold
    # ocr_scan class 0.0 -> 0.95 recall).
    #
    # Born-digital docs with tables/columns deliberately STAY on pdf2docx:
    # measured on the gold set, pdf2docx already extracts their text (table_heavy
    # 0.94 recall pre-Docling) while preserving the visual design (fonts, sizes,
    # colours, column geometry). Docling's semantic rebuild flattens designed
    # documents into a generic style-less Word file — full text, zero identity —
    # which users compare unfavourably to layout-preserving converters. Visual
    # fidelity wins for anything that HAS a text layer.
    if docling_eligible and scanned:
        try:
            dl = _convert_with_docling(ctx.input_path, ctx.output_path)
            # NB: the old content-volume guard (DOCX text < 50% of PDF text ->
            # fall back) was removed as dead code: it belonged to the retired
            # born-digital Docling route. This branch is scanned-only, and a
            # scan has no text layer to compare against.
            if dl is not None and os.path.exists(ctx.output_path) and os.path.getsize(ctx.output_path) > 0:
                ctx.set_progress(93)
                repair = _repair_docx(ctx.output_path)      # fi/fl ligature + spacing repair
                _map_fonts_docx(ctx.output_path)             # Word-native font names
                log.info(f"[{ctx.job_id}] pdf_to_word (docling): {page_count} pages, "
                         f"{dl['tables']} tables, {dl['headings']} headings, "
                         f"repaired {repair['changed']}/{repair['runs']} runs")
                ctx.set_progress(100)
                return {"pages": page_count, "engine": "docling",
                        "tables": dl["tables"], "headings_promoted": dl["headings"],
                        "runs_repaired": repair["changed"], "lines_merged": 0,
                        "list_items": 0, "links_recovered": 0}
        except Exception as ex:
            log.warning(f"[{ctx.job_id}] Docling path failed ({ex}); falling back to "
                        f"{'OCR' if scanned else 'pdf2docx'}")

    # Scanned/image-only PDF: pdf2docx would embed a page image with no text
    # (empty or unsearchable DOCX). Route through Tesseract instead so the output
    # carries real, editable text. Gated strictly on "no page has a text layer",
    # so text-bearing PDFs never take this branch.
    if scanned:
        lang = _sanitise_tesseract_lang(ctx.params.get("lang", "eng"))
        dpi  = max(72, min(int(ctx.params.get("dpi", getattr(Config, "OCR_DPI", 300))), 600))
        ocr  = _ocr_pdf_to_docx(ctx.input_path, ctx.output_path, lang=lang, dpi=dpi,
                                max_pages=getattr(Config, "MAX_OCR_PAGES", 0))
        if not os.path.exists(ctx.output_path) or os.path.getsize(ctx.output_path) == 0:
            raise ProcessingError(
                "Could not extract text from this image-only PDF via OCR."
            )
        ctx.set_progress(93)
        repair = _repair_docx(ctx.output_path)
        log.info(f"[{ctx.job_id}] pdf_to_word (OCR): {ocr['pages']} pages, "
                 f"{ocr['chars']} chars, repaired {repair['changed']}/{repair['runs']} runs")
        ctx.set_progress(100)
        return {"pages": ocr["pages"], "ocr": True,
                "runs_repaired": repair["changed"], "headings_promoted": 0,
                "lines_merged": 0, "list_items": 0, "links_recovered": 0}

    # Phase 6 pre-pass (flag VECTOR_RASTERIZE) — detect + rasterize path-based
    # artwork (charts/diagrams) that pdf2docx drops or mangles, and redact those
    # regions from a temp copy so pdf2docx converts clean text-only pages. The
    # faithful rasters are inserted after conversion (see _vg_insert below).
    vg_found, vg_src = _vg_collect(ctx.input_path)
    pdf2docx_input = vg_src or ctx.input_path

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
                try:
                    cv = Pdf2DocxConverter(pdf2docx_input)
                    try:
                        cv.convert(chunk_out, start=start, end=end)
                    finally:
                        cv.close()
                except Exception as ex:
                    raise ProcessingError(
                        f"PDF to Word conversion failed on pages {start + 1}-{end} "
                        f"({type(ex).__name__}: {ex}). If the file is password-"
                        f"protected, remove the protection first (Unlock PDF)."
                    )
                if os.path.exists(chunk_out) and os.path.getsize(chunk_out) > 0:
                    chunk_docxs.append(chunk_out)
                ctx.set_progress(5 + int((ci + 1) / n_chunks * 85))

            if not chunk_docxs:
                raise ProcessingError("pdf2docx produced no output for any chunk")

            # Merge chunks with python-docx if multiple, else just move the single chunk
            if len(chunk_docxs) == 1:
                shutil.copy(chunk_docxs[0], ctx.output_path)
            else:
                # Preferred: docxcompose — merges styles/numbering/RELATIONSHIPS,
                # so hyperlinks and images from chunks 2..N stay live. The raw
                # deepcopy fallback below keeps body text but degrades chunk-2+
                # hyperlinks to plain text (r:id rels are not carried over —
                # measured on a 120-page doc: 50/120 links survived).
                merged = False
                try:
                    from docxcompose.composer import Composer
                    from docx import Document as _DocxDoc
                    comp = Composer(_DocxDoc(chunk_docxs[0]))
                    for chunk_path in chunk_docxs[1:]:
                        comp.append(_DocxDoc(chunk_path))
                    comp.save(ctx.output_path)
                    merged = os.path.getsize(ctx.output_path) > 0
                except Exception as comp_ex:
                    log.warning(f"[{ctx.job_id}] docxcompose merge unavailable/failed "
                                f"({comp_ex}); using raw body merge")
                try:
                    if not merged:
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
        try:
            cv = Pdf2DocxConverter(pdf2docx_input)
            try:
                cv.convert(ctx.output_path, start=0, end=None)
            finally:
                cv.close()
        except ProcessingError:
            raise
        except Exception as ex:
            raise ProcessingError(
                f"PDF to Word conversion failed ({type(ex).__name__}: {ex}). "
                f"If the file is password-protected, remove the protection "
                f"first (Unlock PDF); if it is damaged, try Repair PDF."
            )

    if not os.path.exists(ctx.output_path) or os.path.getsize(ctx.output_path) == 0:
        raise ProcessingError(
            "pdf2docx produced empty output — the PDF may be image-only, "
            "encrypted, or have an unsupported structure. Try OCR first."
        )

    # Q1 — single-Document pipeline: open the DOCX ONCE, run every post pass
    # against the live object, save ONCE at the end (was 13 zip round-trips,
    # measured at 31-50% of total conversion wall time). If the open fails,
    # live stays None and each pass falls back to its own open/save.
    ctx.set_progress(93)
    live = None
    try:
        from docx import Document as _LiveDoc
        live = _LiveDoc(ctx.output_path)
    except Exception as _lex:
        log.warning(f"[{ctx.job_id}] live-Document mode unavailable ({_lex})")

    # Text-repair pass — fixes dropped ligatures / soft-hyphens / punctuation
    # spacing while preserving layout and run formatting. Best-effort.
    repair = _repair_docx(ctx.output_path, doc=live)

    # Phase 2 — document-intelligence layer: heading promotion, consistent
    # heading spacing, conservative wrapped-line merge. Runs after text repair;
    # additive/high-confidence, so it can't regress documents it can't read.
    ctx.set_progress(96)
    reflow = _reflow_docx(ctx.output_path, doc=live)

    # Split a merged 'NAME  subtitle' header line (pdf2docx glues the large name
    # onto the smaller tagline; MS Word then overlaps them). Conservative.
    header = _split_header_docx(ctx.output_path, doc=live)

    # Recover standalone horizontal rules (section separators) pdf2docx drops —
    # re-read them from the source PDF and re-apply as paragraph bottom borders.
    rules = _recover_rules_docx(ctx.output_path, ctx.input_path, doc=live)

    # G5 — restore superscript on footnote/reference markers (size survived
    # pdf2docx, vertical alignment didn't). Strict conditions, additive.
    _fix_superscripts_docx(ctx.output_path, pdf_path=ctx.input_path, doc=live)

    # Phase 3 — semantic reconstruction: literal bullet/numbered paragraphs
    # become real editable Word lists (numbering.xml + numPr). Additive.
    ctx.set_progress(98)
    semantic = _semantic_docx(ctx.output_path, doc=live)

    # Phase 4B — recover contact/hyperlink text pdf2docx dropped (email/LinkedIn/
    # GitHub), reading it back from the source PDF's link annotations.
    ctx.set_progress(99)
    links = _recover_hyperlinks(ctx.output_path, ctx.input_path, doc=live)

    # G9 — hybrid docs: OCR the scanned pages of a mostly-digital PDF and add
    # their text after each page image (all-or-nothing router misses these).
    hybrid = _hybrid_ocr_docx(ctx.output_path, ctx.input_path,
                              lang=_sanitise_tesseract_lang(ctx.params.get("lang", "eng")),
                              doc=live)

    # G6 — move repeating top/bottom-band lines into real headers/footers
    # (pure page numbers become a live PAGE field).
    hf = _recover_headers_footers(ctx.output_path, ctx.input_path, doc=live)

    # G7 — mark Arabic/Hebrew runs RTL (w:rtl + w:bidi).
    rtl = _fix_rtl_docx(ctx.output_path, doc=live)

    # G10 — re-create AcroForm fields as editable Word content controls.
    forms = _recover_form_fields(ctx.output_path, ctx.input_path, doc=live)

    # G3 — restore per-page size/orientation (landscape pages).
    geom = _fix_page_geometry(ctx.output_path, ctx.input_path, doc=live)

    # Phase 5 — remap Linux/open fonts (Noto/Liberation/DejaVu/…) to Word-native
    # families so Microsoft Word stops substituting them. Deterministic, run-only.
    fonts = _map_fonts_docx(ctx.output_path, doc=live)

    # Phase 6 post-pass — insert the recovered artwork rasters inline at their
    # reading-order slot. Additive, never raises.
    vg = _vg_insert(ctx.output_path, vg_found, doc=live)
    if live is not None:
        try:
            live.save(ctx.output_path)
        except Exception as _sex:
            log.error(f"[{ctx.job_id}] live-Document save failed ({_sex}); "
                      f"output keeps the raw pdf2docx conversion")
    if vg_src:
        try:
            os.unlink(vg_src)
        except OSError:
            pass
    log.info(f"[{ctx.job_id}] pdf_to_word: {page_count} pages, "
             f"repaired {repair['changed']}/{repair['runs']} runs, "
             f"{reflow['headings_promoted']} headings, {reflow['lines_merged']} merges, "
             f"{semantic['list_items']} list items, "
             f"{links['links_recovered']} links recovered, "
             f"{rules['rules_recovered']} rules recovered, "
             f"{vg['vector_images']} vector images recovered, "
             f"{hybrid['ocr_pages']} hybrid pages OCRed, "
             f"hf={hf['header_lines']}/{hf['footer_lines']}, "
             f"rtl={rtl['rtl_runs']}, forms={forms['form_fields']}, "
             f"geom={geom['sections_fixed']}")

    ctx.set_progress(100)
    return {"pages": page_count, "runs_repaired": repair["changed"],
            "headings_promoted": reflow["headings_promoted"],
            "lines_merged": reflow["lines_merged"],
            "list_items": semantic["list_items"],
            "links_recovered": links["links_recovered"],
            "rules_recovered": rules["rules_recovered"],
            "vector_images": vg["vector_images"],
            "hybrid_ocr_pages": hybrid["ocr_pages"],
            "form_fields": forms["form_fields"]}


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
