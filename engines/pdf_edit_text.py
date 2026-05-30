"""
engines/pdf_edit_text.py — PDFWala Enterprise

PDF text editor round-trip:

    /api/pdf/edit-text/load
        Input  : PDF file (uploaded as 'file')
        Steps  : PDF --(pdf2docx)--> DOCX --(mammoth)--> HTML --(sanitise)--> safe HTML
        Output : { success, html, page_count, char_count, warnings }

    /api/pdf/edit-text/save
        Input  : edited HTML (form field 'html'); optional 'page_size' (a4|letter)
        Steps  : HTML --(wrap in document)--> .html file --(LibreOffice)--> PDF
        Output : standard success payload with download_url + filename

The endpoints are deliberately synchronous: the user is waiting on both calls
in the browser, and both operations are bounded (small DOCX + LibreOffice
under a few seconds for any reasonable document). For very large PDFs we
still chunk the pdf2docx step the same way pdf_to_word does.
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional, Tuple

from flask import jsonify, Request

from config import Config
from core.context import JobContext
from core.exceptions import ProcessingError, ValidationError
from core.result import Result
from services.file_service import file_service
from services.redis_service import redis_service

log = logging.getLogger("pdfwala.engines.pdf_edit_text")

# ── Optional deps ───────────────────────────────────────────────────────────
try:
    from pdf2docx import Converter as Pdf2DocxConverter
    PDF2DOCX_OK = True
except ImportError:
    PDF2DOCX_OK = False

try:
    import mammoth
    MAMMOTH_OK = True
except ImportError:
    MAMMOTH_OK = False

try:
    import bleach
    BLEACH_OK = True
except ImportError:
    BLEACH_OK = False

try:
    from bs4 import BeautifulSoup
    BS4_OK = True
except ImportError:
    BS4_OK = False

# Reuse the office engine's hardened LibreOffice runner so we benefit from the
# same isolation, env tweaks and timeout handling.
try:
    from engines.office_engine import _libre  # type: ignore
    LIBRE_OK = True
except Exception:
    LIBRE_OK = False


# ── Limits ──────────────────────────────────────────────────────────────────
_MAX_LOAD_PDF_BYTES    = 5 * 1024 * 1024   # 5 MB hard cap on input PDF for /load
_MAX_HTML_BYTES        = 5 * 1024 * 1024   # 5 MB of edited HTML on /save
_MAX_LOAD_PAGE_COUNT   = 50                # refuse to load huge books for edit
_DEFAULT_PAGE_SIZE     = "a4"              # 'a4' | 'letter'

# Allowed HTML tags/attrs for the editor payload (input from user).
# Wide enough for Quill output (rich text, lists, links, alignment) but
# strict enough to block scripts, iframes, on* handlers, etc.
_ALLOWED_TAGS = {
    "p", "br", "hr", "div", "span",
    "h1", "h2", "h3", "h4", "h5", "h6",
    "strong", "b", "em", "i", "u", "s", "strike", "sub", "sup",
    "ul", "ol", "li", "blockquote", "pre", "code",
    "a", "img",
    "table", "thead", "tbody", "tr", "td", "th",
    "font",
}
_ALLOWED_ATTRS = {
    "*":     ["class", "style", "id", "align", "dir"],
    "a":     ["href", "title", "target", "rel"],
    "img":   ["src", "alt", "title", "width", "height"],
    "td":    ["colspan", "rowspan", "align", "valign"],
    "th":    ["colspan", "rowspan", "align", "valign", "scope"],
    "table": ["border", "cellspacing", "cellpadding", "width", "summary"],
    "font":  ["color", "size", "face"],
}
_ALLOWED_CSS = [
    "color", "background-color", "background",
    "font-size", "font-weight", "font-style", "font-family",
    "text-align", "text-decoration", "line-height", "letter-spacing",
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
    "border", "border-top", "border-right", "border-bottom", "border-left",
    "border-color", "border-style", "border-width",
    "width", "height", "max-width", "min-width",
    "list-style", "list-style-type", "display", "vertical-align",
]


def _require(flag: bool, op: str, lib: str) -> None:
    if not flag:
        raise ProcessingError(
            f"{op} requires the '{lib}' package; reinstall requirements.txt"
        )


def _sanitise_html(raw: str) -> str:
    """Strip dangerous tags / event handlers from user-supplied HTML."""
    if not BLEACH_OK:
        # Fallback: minimal manual strip; better than nothing if bleach missing.
        import re
        out = re.sub(r"<\s*script.*?</script\s*>", "", raw, flags=re.I | re.S)
        out = re.sub(r"\son[a-z]+\s*=\s*\"[^\"]*\"", "", out, flags=re.I)
        out = re.sub(r"\son[a-z]+\s*=\s*'[^']*'", "", out, flags=re.I)
        return out

    cleaner = bleach.Cleaner(
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRS,
        css_sanitizer=_make_css_sanitizer(),
        strip=True,
        strip_comments=True,
    )
    return cleaner.clean(raw)


def _make_css_sanitizer():
    """Return a bleach CSSSanitizer constrained to _ALLOWED_CSS, if available."""
    try:
        from bleach.css_sanitizer import CSSSanitizer
        return CSSSanitizer(allowed_css_properties=_ALLOWED_CSS)
    except ImportError:
        return None


def _pdf_to_docx(pdf_path: str, docx_path: str, max_pages: int) -> int:
    """Run pdf2docx with a page cap. Returns the converted page count."""
    _require(PDF2DOCX_OK, "edit-text", "pdf2docx")
    # pdf2docx writes verbose logging to stdout — keep it but route to logger
    cv = Pdf2DocxConverter(pdf_path)
    try:
        # pdf2docx 0.5.x: convert(out_file, start, end, pages)
        cv.convert(docx_path, start=0, end=max_pages)
    finally:
        try:
            cv.close()
        except Exception:
            pass
    if not os.path.exists(docx_path) or os.path.getsize(docx_path) < 100:
        raise ProcessingError("pdf2docx produced an empty DOCX — file may be image-only or corrupted")
    return max_pages


def _docx_to_html(docx_path: str) -> Tuple[str, list]:
    """Use mammoth to convert DOCX to clean HTML. Images become inline data URIs."""
    _require(MAMMOTH_OK, "edit-text", "mammoth")
    # Convert embedded images to base64 data URIs so the editor can show them
    # without needing a second round-trip to fetch them.
    def _inline_image(image):
        with image.open() as src:
            data = src.read()
        import base64
        b64 = base64.b64encode(data).decode("ascii")
        return {"src": f"data:{image.content_type};base64,{b64}"}

    with open(docx_path, "rb") as fh:
        result = mammoth.convert_to_html(
            fh,
            convert_image=mammoth.images.img_element(_inline_image),
        )
    html = result.value
    msgs = [str(m) for m in (result.messages or [])][:20]
    return html, msgs


def _pretty_html(raw: str) -> str:
    """Minor cleanup so the editor sees nicely-structured paragraphs."""
    if not BS4_OK:
        return raw
    soup = BeautifulSoup(raw, "lxml" if _has_lxml() else "html.parser")
    # Drop empty <p></p> created by mammoth at the start/end
    for p in soup.find_all("p"):
        if not p.get_text(strip=True) and not p.find("img"):
            p.decompose()
    return str(soup)


def _has_lxml() -> bool:
    try:
        import lxml  # noqa: F401
        return True
    except ImportError:
        return False


# ── Endpoint impls ──────────────────────────────────────────────────────────

def pdf_to_editor_html(ctx: JobContext):
    """Implements /api/pdf/edit-text/load."""
    _require(PDF2DOCX_OK, "edit-text", "pdf2docx")
    _require(MAMMOTH_OK,  "edit-text", "mammoth")

    if not ctx.input_path or not os.path.exists(ctx.input_path):
        raise ValidationError("Upload failed — no input file received.")

    # 5 MB cap — keep PDF→DOCX→HTML responsive and the editor instant.
    in_size = os.path.getsize(ctx.input_path)
    if in_size > _MAX_LOAD_PDF_BYTES:
        raise ValidationError(
            f"File is {in_size / (1024*1024):.1f} MB. Edit PDF supports files "
            f"up to {_MAX_LOAD_PDF_BYTES // (1024*1024)} MB — try Compress PDF "
            f"first, or use Split PDF to edit one section at a time."
        )

    # Quick page-count guard via PyMuPDF (already imported elsewhere in app)
    page_count = 0
    try:
        import fitz
        d = fitz.open(ctx.input_path); page_count = len(d); d.close()
    except Exception:
        pass
    if page_count > _MAX_LOAD_PAGE_COUNT:
        raise ValidationError(
            f"This PDF has {page_count} pages. The text editor is capped at "
            f"{_MAX_LOAD_PAGE_COUNT} pages to keep editing responsive — please "
            f"use Split PDF first, then edit a smaller piece."
        )

    work = tempfile.mkdtemp(prefix="pdfwala_edit_")
    try:
        docx_path = os.path.join(work, "in.docx")
        t0 = time.perf_counter()
        _pdf_to_docx(ctx.input_path, docx_path, max_pages=page_count or _MAX_LOAD_PAGE_COUNT)
        t1 = time.perf_counter()
        html_raw, warnings = _docx_to_html(docx_path)
        html = _pretty_html(html_raw)
        t2 = time.perf_counter()
    finally:
        shutil.rmtree(work, ignore_errors=True)

    char_count = len(BeautifulSoup(html, "html.parser").get_text()) if BS4_OK else len(html)
    log.info(
        f"[{ctx.job_id}] edit-text/load pages={page_count} "
        f"pdf2docx={t1 - t0:.2f}s mammoth={t2 - t1:.2f}s html_chars={len(html)}"
    )

    # Persist a hint in Redis so we can correlate /save back if needed
    try:
        redis_service.job_set(ctx.job_id, {
            "operation":   "edit_text_load",
            "page_count":  page_count,
            "status":      "completed",
            "loaded_at":   int(time.time()),
        })
    except Exception:
        pass

    return jsonify({
        "success":    True,
        "job_id":     ctx.job_id,
        "page_count": page_count,
        "char_count": char_count,
        "html":       html,
        "warnings":   warnings,
    }), 200


def editor_html_to_pdf(ctx: JobContext, request: Request):
    """Implements /api/pdf/edit-text/save."""
    _require(LIBRE_OK, "edit-text", "office_engine._libre")

    # Accept HTML from form OR JSON body
    html = (request.form.get("html") or "").strip()
    if not html and request.is_json:
        data = request.get_json(silent=True) or {}
        html = (data.get("html") or "").strip()
    if not html:
        raise ValidationError("No HTML content provided. Send 'html' as a form field.")
    if len(html.encode("utf-8", errors="replace")) > _MAX_HTML_BYTES:
        raise ValidationError(
            f"Edited document is too large ({len(html)} bytes; limit "
            f"{_MAX_HTML_BYTES // (1024 * 1024)} MB)."
        )

    page_size = (request.form.get("page_size") or _DEFAULT_PAGE_SIZE).lower()
    if page_size not in {"a4", "letter"}:
        page_size = _DEFAULT_PAGE_SIZE
    title = (request.form.get("title") or "Edited Document")[:120]
    filename_hint = (request.form.get("filename") or "edited").strip()[:80] or "edited"

    safe = _sanitise_html(html)
    page_css = {
        "a4":     "@page { size: A4; margin: 18mm; }",
        "letter": "@page { size: Letter; margin: 0.75in; }",
    }[page_size]

    full_html = (
        "<!DOCTYPE html>\n<html lang=\"en\"><head>"
        "<meta charset=\"utf-8\"/>"
        f"<title>{_html_escape(title)}</title>"
        "<style>"
        f"{page_css}"
        "body { font-family: 'Liberation Serif','DejaVu Serif',serif; "
        "  font-size: 11pt; color: #111; line-height: 1.45; }"
        "h1,h2,h3,h4,h5,h6 { font-family: 'Liberation Sans','DejaVu Sans',sans-serif; }"
        "p { margin: 0 0 0.6em 0; }"
        "table { border-collapse: collapse; margin: 0.5em 0; }"
        "td, th { border: 1px solid #888; padding: 4px 6px; }"
        "img { max-width: 100%; height: auto; }"
        "blockquote { border-left: 3px solid #ccc; margin: 0.5em 0; padding: 0 0 0 1em; color:#333; }"
        "pre, code { font-family: 'DejaVu Sans Mono', monospace; font-size: 10pt; }"
        "ul, ol { padding-left: 1.5em; }"
        "hr { border: 0; border-top: 1px solid #ccc; margin: 1em 0; }"
        "</style></head><body>"
        f"{safe}"
        "</body></html>"
    )

    # Pick output_path via the same file_service contract used by other ops
    file_service.resolve_output_path(ctx, "pdf")

    work = tempfile.mkdtemp(prefix="pdfwala_edit_save_")
    try:
        html_path = os.path.join(work, f"{filename_hint}.html")
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(full_html)

        out_dir = tempfile.mkdtemp(prefix="lo_out_", dir=work)
        t0 = time.perf_counter()
        converted = _libre(html_path, "pdf", out_dir)
        t1 = time.perf_counter()
        if not converted:
            raise ProcessingError(
                "LibreOffice could not render the edited document to PDF. "
                "If you pasted complex formatting, try simplifying it and saving again."
            )
        # Move to OUTPUT_FOLDER under the resolved name
        shutil.move(converted, ctx.output_path)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    size = os.path.getsize(ctx.output_path)
    fname = os.path.basename(ctx.output_path)
    log.info(
        f"[{ctx.job_id}] edit-text/save libreoffice={t1 - t0:.2f}s "
        f"out_size={size} ({fname})"
    )

    return jsonify({
        "success":      True,
        "message":      "PDF saved successfully",
        "job_id":       ctx.job_id,
        "status":       "completed",
        "download_url": f"/download/{fname}",
        "filename":     fname,
        "size_bytes":   size,
        "size_human":   _fmt_size(size),
        "expires_in":   f"{Config.FILE_TTL_SEC // 60} minutes",
    }), 200


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace("\"", "&quot;")
    )


def _fmt_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024:
            return f"{f:.1f} {unit}"
        f /= 1024
    return f"{f:.1f} TB"
