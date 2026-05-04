"""
engines/pdf_engine.py — PDFWala Enterprise V12.0

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
"""

import io
import json
import logging
import os
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import List

from config import Config
from core.context import JobContext
from core.exceptions import (
    ProcessingError, ValidationError, UnsupportedOperation, OperationTimeoutError
)
from core.pipeline import register
from utils.helpers import format_file_size
from utils.pdf_utils import (
    parse_page_ranges, create_watermark_pdf, create_page_number_pdf,
    compress_pdf_images,
)
from utils.security import REDACTION_PATTERNS, SafeRegex

log = logging.getLogger("pdfwala.engines.pdf")

# ── Library availability flags ────────────────────────────────────────────────
try:
    import fitz
    FITZ_OK = True
except ImportError:
    FITZ_OK = False

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

try:
    import difflib
    DIFFLIB_OK = True
except ImportError:
    DIFFLIB_OK = False


# ── Internal helpers (private to this engine) ─────────────────────────────────

def _require(flag: bool, operation: str, library: str):
    if not flag:
        raise UnsupportedOperation(operation, library)


def _ghostscript(input_path: str, output_path: str,
                 gs_setting: str = "/ebook",
                 extra_flags: list = None,
                 timeout: int = None) -> bool:
    """Run Ghostscript. Returns True on success. Raises ProcessingError on timeout."""
    timeout = timeout or Config.SUBPROCESS_TIMEOUT
    if output_path.startswith("-"):
        raise ValidationError("Invalid output path")
    cmd = [
        Config.GHOSTSCRIPT,
        "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.5",
        f"-dPDFSETTINGS={gs_setting}",
        "-dNOPAUSE", "-dBATCH", "-dQUIET", "-dSAFER",
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true", "-dSubsetFonts=true",
        "-dAutoRotatePages=/None",
        f"-sOutputFile={output_path}",
    ]
    if extra_flags:
        cmd.extend(extra_flags)
    cmd.append(input_path)
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if result.returncode != 0:
            log.warning(f"GS rc={result.returncode}: {result.stderr[:200]}")
            return False
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return False
        return True
    except subprocess.TimeoutExpired:
        try: os.remove(output_path)
        except OSError: pass
        raise OperationTimeoutError("Ghostscript", timeout)


def _guard_empty(path: str):
    if not FITZ_OK:
        return
    doc = fitz.open(path)
    n = len(doc)
    doc.close()
    if n == 0:
        raise ValidationError("Input PDF has no pages")


# ═══════════════════════════════════════════════════════════════════════════════
# PDF ORGANIZE
# ═══════════════════════════════════════════════════════════════════════════════

@register("merge_pdf")
def merge_pdf(ctx: JobContext) -> dict:
    _require(PYPDF2_OK, "merge_pdf", "PyPDF2")
    paths = ctx.input_paths
    merger = PdfMerger()
    page_sizes = set()
    for p in paths:
        if not FITZ_OK:
            merger.append(p)
            continue
        doc = fitz.open(p)
        if len(doc) == 0:
            doc.close()
            raise ValidationError(f"PDF {os.path.basename(p)} has no pages")
        if doc.is_encrypted and not doc.authenticate(""):
            doc.close()
            raise ValidationError(f"PDF {os.path.basename(p)} is password-protected")
        for pg in doc:
            page_sizes.add((round(pg.rect.width), round(pg.rect.height)))
        doc.close()
        merger.append(p)
    merger.write(ctx.output_path)
    merger.close()
    return {"mixed_page_sizes": len(page_sizes) > 1, "files_merged": len(paths)}


@register("split_pdf")
def split_pdf(ctx: JobContext) -> dict:
    _require(PYPDF2_OK, "split_pdf", "PyPDF2")
    mode   = ctx.params.get("mode", "all")
    ranges = ctx.params.get("ranges", "")
    _guard_empty(ctx.input_path)
    reader = PdfReader(ctx.input_path)
    total  = len(reader.pages)
    indices = list(range(total)) if mode == "all" else parse_page_ranges(ranges, total)
    if not indices:
        raise ValidationError("No valid pages in range")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for idx in indices:
            w = PdfWriter()
            w.add_page(reader.pages[idx])
            pb = io.BytesIO()
            w.write(pb)
            zf.writestr(f"page_{idx + 1:04d}.pdf", pb.getvalue())
    with open(ctx.output_path, "wb") as fh:
        fh.write(buf.getvalue())
    return {"pages_exported": len(indices)}


@register("organize_pdf")
def organize_pdf(ctx: JobContext) -> dict:
    _require(PYPDF2_OK, "organize_pdf", "PyPDF2")
    action     = ctx.params.get("action", "reorder")
    order_spec = ctx.params.get("order", "")
    _guard_empty(ctx.input_path)
    reader = PdfReader(ctx.input_path)
    total  = len(reader.pages)
    indices = parse_page_ranges(order_spec, total)
    zero = [i - 1 for i in indices]
    if action == "delete":
        keep = [i for i in range(total) if i not in set(zero)]
        if not keep:
            raise ValidationError("Cannot delete all pages")
    else:
        keep = zero
    w = PdfWriter()
    for i in keep:
        if i < 0 or i >= total:
            raise ValidationError(f"Page {i + 1} out of range")
        w.add_page(reader.pages[i])
    tmp = ctx.output_path + ".tmp"
    with open(tmp, "wb") as fh:
        w.write(fh)
    os.replace(tmp, ctx.output_path)
    return {"action": action, "pages_in_output": len(keep)}


@register("remove_pages")
def remove_pages(ctx: JobContext) -> dict:
    _require(PYPDF2_OK, "remove_pages", "PyPDF2")
    _guard_empty(ctx.input_path)
    reader = PdfReader(ctx.input_path)
    total  = len(reader.pages)
    remove = set(parse_page_ranges(ctx.params.get("order", ""), total))
    if len(remove) >= total:
        raise ValidationError("Cannot remove all pages")
    w = PdfWriter()
    for i, page in enumerate(reader.pages):
        if i not in remove:
            w.add_page(page)
    with open(ctx.output_path, "wb") as fh:
        w.write(fh)
    return {"pages_removed": len(remove)}


@register("extract_pages")
def extract_pages(ctx: JobContext) -> dict:
    _require(PYPDF2_OK, "extract_pages", "PyPDF2")
    _guard_empty(ctx.input_path)
    reader  = PdfReader(ctx.input_path)
    total   = len(reader.pages)
    indices = parse_page_ranges(ctx.params.get("order", ""), total)
    if not indices:
        raise ValidationError("No valid pages in range")
    w = PdfWriter()
    for idx in indices:
        w.add_page(reader.pages[idx])
    with open(ctx.output_path, "wb") as fh:
        w.write(fh)
    return {"pages_extracted": len(indices)}


# ═══════════════════════════════════════════════════════════════════════════════
# PDF OPTIMIZE
# ═══════════════════════════════════════════════════════════════════════════════

@register("compress_pdf")
def compress_pdf(ctx: JobContext) -> dict:
    _require(FITZ_OK and PIL_OK, "compress_pdf", "PyMuPDF + Pillow")
    quality = ctx.params.get("quality", "medium")
    cfg = {
        "low":    {"dpi": 150, "quality": 85, "gs": "/printer"},
        "medium": {"dpi": 120, "quality": 72, "gs": "/printer"},
        "high":   {"dpi": 96,  "quality": 60, "gs": "/ebook"},
    }.get(quality, {"dpi": 120, "quality": 72, "gs": "/printer"})

    _guard_empty(ctx.input_path)
    orig = os.path.getsize(ctx.input_path)

    # Stage 1: PyMuPDF image downsampling
    stage1 = ctx.output_path + "_s1.pdf"
    try:
        doc = fitz.open(ctx.input_path)
        modified = compress_pdf_images(doc, cfg["dpi"], cfg["quality"])
        if modified:
            doc.save(stage1, deflate=True, deflate_images=True,
                     deflate_fonts=True, garbage=3, clean=False)
        else:
            shutil.copy(ctx.input_path, stage1)
        doc.close()
    except Exception as ex:
        log.warning(f"Stage1 failed: {ex}")
        shutil.copy(ctx.input_path, stage1)

    stage1_size = os.path.getsize(stage1)

    # Stage 2: Ghostscript
    gs_out = ctx.output_path + "_gs.pdf"
    try:
        gs_ok = _ghostscript(
            stage1, gs_out, cfg["gs"],
            extra_flags=[
                "-dColorImageDownsampleType=/Bicubic",
                f"-dColorImageResolution={cfg['dpi']}",
                f"-dGrayImageResolution={cfg['dpi']}",
            ],
        )
    except OperationTimeoutError:
        gs_ok = False

    chosen = None
    if gs_ok and os.path.exists(gs_out) and os.path.getsize(gs_out) < stage1_size:
        chosen = gs_out
    if not chosen and stage1_size < orig:
        chosen = stage1
    if not chosen:
        chosen = ctx.input_path

    shutil.copy(chosen, ctx.output_path)

    for tmp in [stage1, gs_out]:
        try: os.remove(tmp)
        except OSError: pass

    new_size  = os.path.getsize(ctx.output_path)
    reduction = round((1 - new_size / orig) * 100, 1) if orig else 0
    return {
        "reduction_pct": reduction,
        "original_size_bytes": orig,
        "compressed_size_bytes": new_size,
    }


@register("repair_pdf")
def repair_pdf(ctx: JobContext) -> dict:
    _require(FITZ_OK, "repair_pdf", "PyMuPDF")
    orig_size = os.path.getsize(ctx.input_path)

    # Stage 1: PyMuPDF
    try:
        doc = fitz.open(ctx.input_path)
        pages = len(doc)
        if pages == 0:
            doc.close()
            raise ValidationError("Input PDF has no pages")
        tmp = ctx.output_path + ".tmp"
        doc.save(tmp, garbage=4, deflate=True, clean=True)
        doc.close()
        os.replace(tmp, ctx.output_path)
        if os.path.getsize(ctx.output_path) > 0:
            return {"method": "pymupdf", "pages": pages, "original_size_bytes": orig_size}
    except PDFWalaError:
        raise
    except Exception as ex:
        log.warning(f"PyMuPDF repair failed: {ex}")

    # Stage 2: Ghostscript
    gs_tmp = ctx.output_path + "_gs.pdf"
    try:
        gs_ok = _ghostscript(ctx.input_path, gs_tmp, "/printer",
                              extra_flags=["-dPDFSTOPONERROR=false"])
        if gs_ok and os.path.getsize(gs_tmp) > 0:
            os.replace(gs_tmp, ctx.output_path)
            return {"method": "ghostscript", "original_size_bytes": orig_size}
    except Exception:
        pass
    finally:
        try: os.remove(gs_tmp)
        except OSError: pass

    shutil.copy(ctx.input_path, ctx.output_path)
    return {"method": "passthrough", "note": "PDF was already valid"}


@register("linearize_pdf")
def linearize_pdf(ctx: JobContext) -> dict:
    _guard_empty(ctx.input_path)
    orig = os.path.getsize(ctx.input_path)
    ok = _ghostscript(ctx.input_path, ctx.output_path, "/printer")
    if not ok or not os.path.exists(ctx.output_path):
        raise ProcessingError("Linearization failed — check Ghostscript installation")
    new_size  = os.path.getsize(ctx.output_path)
    reduction = round((1 - new_size / orig) * 100, 1) if orig else 0
    return {"reduction_pct": reduction, "original_size_bytes": orig}


# ═══════════════════════════════════════════════════════════════════════════════
# PDF EDIT
# ═══════════════════════════════════════════════════════════════════════════════

@register("rotate_pdf")
def rotate_pdf(ctx: JobContext) -> dict:
    _require(FITZ_OK, "rotate_pdf", "PyMuPDF")
    angle      = ctx.params.get("angle", 90)
    pages_spec = ctx.params.get("pages", "all")
    _guard_empty(ctx.input_path)
    doc   = fitz.open(ctx.input_path)
    total = len(doc)
    idxs  = (list(range(total)) if pages_spec.lower() == "all"
             else parse_page_ranges(pages_spec, total))
    if not idxs:
        doc.close()
        raise ValidationError("No valid pages matched the specified range")
    for i in idxs:
        doc[i].set_rotation(angle)
    doc.save(ctx.output_path)
    doc.close()
    return {"pages_rotated": len(idxs), "angle": angle}


@register("watermark_pdf")
def watermark_pdf(ctx: JobContext) -> dict:
    _require(FITZ_OK, "watermark_pdf", "PyMuPDF")
    text       = ctx.params.get("text", "CONFIDENTIAL")
    color      = ctx.params.get("color", "808080")
    opacity    = float(ctx.params.get("opacity", 0.3))
    position   = ctx.params.get("position", "diagonal")
    rotation   = float(ctx.params.get("rotation", 45.0))
    image_data = ctx.params.get("image_data")  # bytes, pre-loaded by route

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
                scale = ctx.params.get("scale", 0.3)
                iw = r.width * scale
                ih = iw * img.height / img.width
                ix = r.x0 + (r.width - iw) / 2
                iy = r.y0 + (r.height - ih) / 2
                page.insert_image(fitz.Rect(ix, iy, ix + iw, iy + ih),
                                  stream=buf.getvalue(), overlay=True)
            else:
                wm    = create_watermark_pdf(text, opacity, color,
                                             r.width, r.height, position, rotation)
                wmpdf = fitz.open("pdf", wm)
                page.show_pdf_page(fitz.Rect(0, 0, r.width, r.height),
                                   wmpdf, 0, overlay=True)
                wmpdf.close()
        doc.save(ctx.output_path)
    finally:
        doc.close()
    return {"watermark_type": "image" if image_data else "text"}


@register("page_numbers")
def page_numbers(ctx: JobContext) -> dict:
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
            page.show_pdf_page(fitz.Rect(0, 0, r.width, r.height),
                               pnpdf, 0, overlay=True)
            pnpdf.close()
        doc.save(ctx.output_path)
    finally:
        doc.close()
    return {}


@register("crop_pdf")
def crop_pdf(ctx: JobContext) -> dict:
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
            page.set_cropbox(nr)
        doc.save(ctx.output_path)
    finally:
        doc.close()
    return {}


@register("redact_pdf")
def redact_pdf(ctx: JobContext) -> dict:
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
                f"Unknown preset. Choose: {', '.join(REDACTION_PATTERNS)}"
            )
        import re
        compiled = re.compile(REDACTION_PATTERNS[preset_name])

    _guard_empty(ctx.input_path)
    doc = fitz.open(ctx.input_path)
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
        doc.save(ctx.output_path)
    finally:
        doc.close()
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
        meta  = doc.metadata
        sizes = {}
        for pg in doc:
            k = (round(pg.rect.width, 1), round(pg.rect.height, 1))
            sizes[k] = sizes.get(k, 0) + 1
        font_names = set()
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
        return {
            "metadata": {
                "page_count":   len(doc),
                "pdf_version":  str(doc.pdf_version()),
                "title":        meta.get("title", ""),
                "author":       meta.get("author", ""),
                "encrypted":    doc.is_encrypted,
                "file_size_bytes": file_size,
                "size_human":   format_file_size(file_size),
                "has_forms":    any(pg.first_widget for pg in doc),
                "has_toc":      len(doc.get_toc()) > 0,
                "image_count":  sum(len(pg.get_images()) for pg in doc),
                "fonts_used":   sorted(font_names)[:20],
                "is_linearized": is_lin,
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
    _require(FITZ_OK, "protect_pdf", "PyMuPDF")
    pw          = ctx.params.get("password", "")
    allow_print = ctx.params.get("allow_print", True)
    allow_copy  = ctx.params.get("allow_copy", True)
    if not pw:
        raise ValidationError("Password required")
    _guard_empty(ctx.input_path)
    doc = fitz.open(ctx.input_path)
    try:
        permissions = int(fitz.PDF_PERM_ACCESSIBILITY)
        if allow_print: permissions |= int(fitz.PDF_PERM_PRINT)
        if allow_copy:  permissions |= int(fitz.PDF_PERM_COPY)
        doc.save(ctx.output_path,
                 encryption=fitz.PDF_ENCRYPT_AES_256,
                 owner_pw=pw, user_pw=pw,
                 permissions=permissions)
    finally:
        doc.close()
    return {}


@register("unlock_pdf")
def unlock_pdf(ctx: JobContext) -> dict:
    _require(FITZ_OK, "unlock_pdf", "PyMuPDF")
    pw = ctx.params.get("password", "")
    if not pw:
        raise ValidationError("Password required")
    doc = fitz.open(ctx.input_path)
    if doc.is_encrypted and not doc.authenticate(pw):
        doc.close()
        raise ValidationError("Wrong password")
    doc.save(ctx.output_path, encryption=fitz.PDF_ENCRYPT_NONE)
    doc.close()
    return {}


@register("sign_pdf")
def sign_pdf(ctx: JobContext) -> dict:
    _require(FITZ_OK, "sign_pdf", "PyMuPDF")
    from datetime import datetime
    name        = ctx.params.get("name", "Signed")
    reason      = ctx.params.get("reason", "Approved")
    page_target = ctx.params.get("page", "last")
    position    = ctx.params.get("position", "bottom-right")
    sig_data    = ctx.params.get("sig_data")
    today_str   = datetime.now().strftime("%Y-%m-%d")

    _guard_empty(ctx.input_path)
    doc   = fitz.open(ctx.input_path)
    total = len(doc)
    try:
        if page_target == "all":    idxs = list(range(total))
        elif page_target == "first": idxs = [0]
        elif page_target == "last":  idxs = [total - 1]
        else:
            try:
                n = int(page_target)
                if n < 1 or n > total:
                    raise ValidationError(f"Page {n} out of range (1-{total})")
                idxs = [n - 1]
            except ValueError:
                raise ValidationError(f"Invalid page target: '{page_target}'")

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
            if sig_data:
                page.insert_image(fitz.Rect(sx, sy - 40, sx + 150, sy + 5),
                                  stream=sig_data, overlay=True)
            line  = f"{name} | {reason} | {today_str}"
            box_r = fitz.Rect(sx - 5, sy - 5, sx + 155, sy + 25)
            page.draw_rect(box_r, color=(0, 0, 0.6), fill=(0.9, 0.9, 1), width=0.5)
            page.insert_text((sx, sy + 12), line, fontsize=8, color=(0, 0, 0.5))

        doc.save(ctx.output_path)
    finally:
        doc.close()
    return {"pages_signed": len(idxs)}


# ═══════════════════════════════════════════════════════════════════════════════
# PDF CONVERT
# ═══════════════════════════════════════════════════════════════════════════════

@register("pdf_to_image")
def pdf_to_image(ctx: JobContext) -> dict:
    _require(FITZ_OK and PIL_OK, "pdf_to_image", "PyMuPDF + Pillow")
    fmt = ctx.params.get("format", "jpg").lower()
    dpi = int(ctx.params.get("dpi", 150))
    if fmt not in ("jpg", "png"): fmt = "jpg"
    _guard_empty(ctx.input_path)
    doc = fitz.open(ctx.input_path)
    try:
        count = len(doc)
        buf   = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, page in enumerate(doc):
                mat = fitz.Matrix(dpi / 72, dpi / 72)
                pix = page.get_pixmap(matrix=mat, alpha=True)
                pil = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGBA")
                bg  = Image.new("RGB", pil.size, (255, 255, 255))
                bg.paste(pil, mask=pil.split()[3])
                ib  = io.BytesIO()
                if fmt == "jpg":
                    bg.save(ib, "JPEG", quality=85, optimize=True)
                else:
                    bg.save(ib, "PNG")
                zf.writestr(f"page_{i + 1:04d}.{fmt}", ib.getvalue())
        with open(ctx.output_path, "wb") as fh:
            fh.write(buf.getvalue())
    finally:
        doc.close()
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
    _require(PDF2DOCX_OK, "pdf_to_word", "pdf2docx")
    cv = Pdf2DocxConverter(ctx.input_path)
    try:
        cv.convert(ctx.output_path, start=0, end=None)
    finally:
        cv.close()
    if not os.path.exists(ctx.output_path) or os.path.getsize(ctx.output_path) == 0:
        raise ProcessingError("pdf2docx produced empty output")
    return {}


@register("pdf_to_excel")
def pdf_to_excel(ctx: JobContext) -> dict:
    _require(OPENPYXL_OK, "pdf_to_excel", "openpyxl")
    wb = Workbook()
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
        for pn, pg in enumerate(doc):
            ws.cell(row_idx, 1, f"--- Page {pn + 1} ---")
            row_idx += 1
            for line in pg.get_text("text").split("\n"):
                if line.strip():
                    ws.cell(row_idx, 1, line.strip())
                    row_idx += 1
        doc.close()

    wb.save(ctx.output_path)
    return {"tables_found": tables_extracted}


@register("pdf_to_ppt")
def pdf_to_ppt(ctx: JobContext) -> dict:
    _require(PPTX_OK and FITZ_OK, "pdf_to_ppt", "python-pptx + PyMuPDF")
    _guard_empty(ctx.input_path)
    doc = fitz.open(ctx.input_path)
    try:
        prs = Presentation()
        prs.slide_width  = PptxInches(10)
        prs.slide_height = PptxInches(7.5)
        blank = prs.slide_layouts[6]
        for page in doc:
            pix     = page.get_pixmap(dpi=200)
            tmp_img = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            tmp_img.write(pix.tobytes("png"))
            tmp_img.close()
            slide = prs.slides.add_slide(blank)
            slide.shapes.add_picture(
                tmp_img.name, 0, 0, prs.slide_width, prs.slide_height
            )
            os.unlink(tmp_img.name)
    finally:
        doc.close()
    prs.save(ctx.output_path)
    return {}


@register("pdf_to_pdfa")
def pdf_to_pdfa(ctx: JobContext) -> dict:
    version   = ctx.params.get("version", "1b")
    pdfa_val  = "2" if "3" in version else "1"
    cmd = [
        Config.GHOSTSCRIPT, "-dBATCH", "-dNOPAUSE", "-dSAFER",
        "-sDEVICE=pdfwrite", f"-dPDFA={pdfa_val}",
        "-dPDFACompatibilityPolicy=1",
        f"-sOutputFile={ctx.output_path}",
        ctx.input_path,
    ]
    timeout = Config.PDFA_TIMEOUT
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if result.returncode != 0:
            raise ProcessingError("Ghostscript PDF/A conversion failed")
    except subprocess.TimeoutExpired:
        raise OperationTimeoutError("PDF/A conversion", timeout)
    return {"pdfa_version": version}


@register("ocr_pdf")
def ocr_pdf(ctx: JobContext) -> dict:
    _require(TESSERACT_OK and FITZ_OK, "ocr_pdf", "pytesseract + PyMuPDF")
    lang = ctx.params.get("lang", "eng")
    dpi  = int(ctx.params.get("dpi", 300))
    psm  = int(ctx.params.get("psm", 3))
    oem  = int(ctx.params.get("oem", 3))

    _guard_empty(ctx.input_path)
    src_doc = fitz.open(ctx.input_path)
    out_doc = fitz.open()
    pages_processed = pages_skipped = 0

    try:
        total = len(src_doc)
        ctx.set_progress(5)
        for page_num, src_page in enumerate(src_doc):
            pw, ph = src_page.rect.width, src_page.rect.height
            mat = fitz.Matrix(dpi / 72, dpi / 72)
            pix = src_page.get_pixmap(matrix=mat, alpha=False, colorspace=fitz.csGRAY)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            img_sx = pw / pix.width
            img_sy = ph / pix.height
            hocr   = None
            try:
                hocr = pytesseract.image_to_data(
                    img, lang=lang,
                    output_type=TesseractOutput.DICT,
                    config=f"--psm {psm} --oem {oem}",
                )
            except Exception as ex:
                log.warning(f"OCR page {page_num + 1}: {ex}")

            new_page = out_doc.new_page(width=pw, height=ph)
            new_page.show_pdf_page(
                fitz.Rect(0, 0, pw, ph), src_doc, page_num, overlay=False
            )
            if hocr:
                for i in range(len(hocr.get("text", []))):
                    word = (hocr["text"][i] or "").strip()
                    conf = int(hocr["conf"][i]) if hocr["conf"][i] != -1 else 0
                    if not word or conf < 30:
                        continue
                    x0 = hocr["left"][i] * img_sx
                    y1 = (hocr["top"][i] + hocr["height"][i]) * img_sy
                    fs = max(4.0, hocr["height"][i] * img_sy * 0.85)
                    new_page.insert_text(
                        (x0, y1 - 1), word + " ",
                        fontsize=fs, fontname="helv",
                        color=(0, 0, 0), render_mode=3, overlay=True,
                    )
            pages_processed += 1
            ctx.set_progress(int((page_num + 1) / total * 95))

        if pages_processed == 0:
            raise ProcessingError("OCR produced no output — all pages failed")

        out_doc.save(ctx.output_path, deflate=True, garbage=2)
    finally:
        out_doc.close()
        src_doc.close()

    return {"pages_processed": pages_processed, "lang": lang, "dpi": dpi}


@register("compare_pdf")
def compare_pdf(ctx: JobContext) -> dict:
    _require(FITZ_OK and PIL_OK, "compare_pdf", "PyMuPDF + Pillow")
    from PIL import ImageChops
    p1, p2 = ctx.input_paths[0], ctx.input_paths[1]
    doc1   = fitz.open(p1)
    doc2   = fitz.open(p2)
    if len(doc1) == 0:
        raise ValidationError("First PDF has no pages")
    if len(doc2) == 0:
        raise ValidationError("Second PDF has no pages")
    try:
        pages     = min(len(doc1), len(doc2))
        buf       = io.BytesIO()
        sims      = []
        diff_data = []
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for i in range(pages):
                pix1 = doc1[i].get_pixmap(dpi=150)
                pix2 = doc2[i].get_pixmap(dpi=150)
                img1 = Image.open(io.BytesIO(pix1.tobytes("png"))).convert("RGB")
                img2 = Image.open(io.BytesIO(pix2.tobytes("png"))).convert("RGB")
                if img1.size != img2.size:
                    img2 = img2.resize(img1.size, Image.LANCZOS)
                diff = ImageChops.difference(img1, img2)
                diff = diff.point(lambda x: min(x * 8, 255))
                db   = io.BytesIO()
                diff.save(db, "PNG")
                zf.writestr(f"diff_page_{i + 1:04d}.png", db.getvalue())
                import difflib
                words1 = [w[4] for w in doc1[i].get_text("words")][:500]
                words2 = [w[4] for w in doc2[i].get_text("words")][:500]
                sm     = difflib.SequenceMatcher(None, words1, words2)
                sim    = round(sm.ratio() * 100, 1)
                sims.append(sim)
                diff_data.append({"page": i + 1, "similarity_pct": sim})
            zf.writestr("summary.json", json.dumps({
                "pages": diff_data,
                "overall_similarity_pct": round(sum(sims) / len(sims), 1) if sims else 0,
            }))
        with open(ctx.output_path, "wb") as fh:
            fh.write(buf.getvalue())
    finally:
        doc1.close()
        doc2.close()
    return {"pages_compared": pages}
