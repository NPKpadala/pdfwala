"""
app/routes/pdf_routes.py — PDFWala Enterprise V13.0
All PDF tool endpoints. Routes ONLY: validate input, build ctx, enqueue/run.
Zero processing logic.
"""

import io
import json
import logging
import os
import shutil
import tempfile

from flask import Blueprint, request, jsonify, send_file

from app.controllers.job_controller import JobController
from config import Config
from core.exceptions import ValidationError
from core.result import Result
from services.file_service import file_service
from tasks.pdf_tasks import PDF_TASK_MAP

pdf_bp = Blueprint("pdf", __name__, url_prefix="/api/pdf")
log = logging.getLogger("pdfwala.routes.pdf")

# ── Helper ─────────────────────────────────────────────────────────────────────

# Operations that are always slow (seconds to minutes even for small files).
# These bypass the file-size threshold and always run on a Celery worker so
# they never tie up a gunicorn web worker — even with the 10 MB upload cap,
# a single sync OCR can hold a request slot for minutes and starve other
# users. The frontend already handles async polling.
_ALWAYS_ASYNC = {
    "ocr_pdf",
    "pdf_to_word",   # pdf2docx can be heavy on dense PDFs
    "pdf_to_excel",
    "pdf_to_ppt",
    "pdf_to_pdfa",   # Ghostscript PDF/A conversion is slow
    "compare_pdf",
}


def _handle(operation: str, output_ext: str, msg: str,
            multi: bool = False, field: str = "file"):
    rl = JobController.check_rate_limit(request)
    if rl:
        return rl
    ctx = JobController.build_ctx(request, operation)
    try:
        if multi:
            size = file_service.save_multiple(request, ctx, field)
        else:
            size = file_service.save_single(request, ctx, field)
    except ValidationError as ex:
        return Result.error(ex.message, 400)
    task_fn = PDF_TASK_MAP.get(operation)
    force_async = operation in _ALWAYS_ASYNC
    return JobController.run_or_enqueue(
        ctx, size, task_fn, output_ext, msg, force_async=force_async,
    )


# ── Organize ───────────────────────────────────────────────────────────────────

@pdf_bp.route("/merge",          methods=["POST"])
def merge_pdf():
    return _handle("merge_pdf", "pdf", "PDFs merged successfully", multi=True, field="files")

@pdf_bp.route("/split",          methods=["POST"])
def split_pdf():
    return _handle("split_pdf", "zip", "PDF split successfully")

@pdf_bp.route("/organize",       methods=["POST"])
def organize_pdf():
    return _handle("organize_pdf", "pdf", "PDF organized successfully")

@pdf_bp.route("/remove-pages",   methods=["POST"])
def remove_pages():
    return _handle("remove_pages", "pdf", "Pages removed successfully")

@pdf_bp.route("/extract-pages",  methods=["POST"])
def extract_pages():
    return _handle("extract_pages", "pdf", "Pages extracted successfully")


# ── Optimize ───────────────────────────────────────────────────────────────────

@pdf_bp.route("/compress",       methods=["POST"])
def compress_pdf():
    return _handle("compress_pdf", "pdf", "PDF compressed successfully")

@pdf_bp.route("/repair",         methods=["POST"])
def repair_pdf():
    return _handle("repair_pdf", "pdf", "PDF repaired successfully")

@pdf_bp.route("/linearize",      methods=["POST"])
def linearize_pdf():
    return _handle("linearize_pdf", "pdf", "PDF linearized successfully")


# ── Edit ───────────────────────────────────────────────────────────────────────

@pdf_bp.route("/rotate",         methods=["POST"])
def rotate_pdf():
    return _handle("rotate_pdf", "pdf", "PDF rotated successfully")

@pdf_bp.route("/watermark",      methods=["POST"])
def watermark_pdf():
    return _handle("watermark_pdf", "pdf", "Watermark added successfully")

@pdf_bp.route("/page-numbers",   methods=["POST"])
def page_numbers():
    return _handle("page_numbers", "pdf", "Page numbers added successfully")

@pdf_bp.route("/crop",           methods=["POST"])
def crop_pdf():
    return _handle("crop_pdf", "pdf", "PDF cropped successfully")

@pdf_bp.route("/redact",         methods=["POST"])
def redact_pdf():
    return _handle("redact_pdf", "pdf", "PDF redacted successfully")

@pdf_bp.route("/edit",           methods=["POST"])
def edit_pdf():
    return _handle("edit_pdf",   "pdf", "PDF edited successfully")


# ── Edit PDF (text-editor flow) ───────────────────────────────────────────────
# Two endpoints that round-trip a PDF through DOCX so the user can edit the
# actual text in a rich-text editor in the browser, then save back to PDF.
#
#   /edit-text/load : POST PDF  →  returns sanitised HTML for the editor
#   /edit-text/save : POST HTML →  returns a fresh PDF (download_url + filename)
#
# These bypass the JobController async machinery — both calls are interactive
# (the user is waiting in the browser) so they run inline and return 200.

@pdf_bp.route("/edit-text/load", methods=["POST"])
def edit_text_load():
    rl = JobController.check_rate_limit(request)
    if rl:
        return rl
    ctx = JobController.build_ctx(request, "edit_text_load")
    try:
        file_service.save_single(request, ctx, "file")
    except ValidationError as ex:
        return Result.error(ex.message, 400)
    try:
        from engines.pdf_edit_text import pdf_to_editor_html
        result = pdf_to_editor_html(ctx)
    except ValidationError as ex:
        return Result.error(ex.message, 400)
    except Exception as ex:
        return Result.error(f"Could not prepare editor: {ex}", 500, ctx.job_id)
    return result


@pdf_bp.route("/edit-text/save", methods=["POST"])
def edit_text_save():
    rl = JobController.check_rate_limit(request)
    if rl:
        return rl
    ctx = JobController.build_ctx(request, "edit_text_save")
    try:
        from engines.pdf_edit_text import editor_html_to_pdf
        result = editor_html_to_pdf(ctx, request)
    except ValidationError as ex:
        return Result.error(ex.message, 400)
    except Exception as ex:
        return Result.error(f"Save failed: {ex}", 500, ctx.job_id)
    return result


# ── Security ───────────────────────────────────────────────────────────────────

@pdf_bp.route("/protect",        methods=["POST"])
def protect_pdf():
    return _handle("protect_pdf", "pdf", "PDF protected successfully")

@pdf_bp.route("/unlock",         methods=["POST"])
def unlock_pdf():
    return _handle("unlock_pdf", "pdf", "PDF unlocked successfully")

@pdf_bp.route("/sign",           methods=["POST"])
def sign_pdf():
    return _handle("sign_pdf", "pdf", "PDF signed successfully")


# ── Info ───────────────────────────────────────────────────────────────────────

@pdf_bp.route("/info",           methods=["POST"])
def pdf_info():
    return _handle("pdf_info", "json", "PDF info extracted")


# ── Convert ───────────────────────────────────────────────────────────────────

@pdf_bp.route("/to-image",       methods=["POST"])
def pdf_to_image():
    fmt = request.form.get("format", "jpg")
    return _handle("pdf_to_image", "zip", "PDF converted to images")

@pdf_bp.route("/to-jpg",         methods=["POST"])
def pdf_to_jpg():
    return _handle("pdf_to_jpg", "zip", "PDF converted to JPG")

@pdf_bp.route("/to-png",         methods=["POST"])
def pdf_to_png():
    return _handle("pdf_to_png", "zip", "PDF converted to PNG")

@pdf_bp.route("/to-word",        methods=["POST"])
def pdf_to_word():
    return _handle("pdf_to_word", "docx", "PDF converted to Word")

@pdf_bp.route("/to-excel",       methods=["POST"])
def pdf_to_excel():
    return _handle("pdf_to_excel", "xlsx", "PDF converted to Excel")

@pdf_bp.route("/to-ppt",         methods=["POST"])
def pdf_to_ppt():
    return _handle("pdf_to_ppt", "pptx", "PDF converted to PowerPoint")

@pdf_bp.route("/to-pdfa",        methods=["POST"])
def pdf_to_pdfa():
    return _handle("pdf_to_pdfa", "pdf", "PDF converted to PDF/A")

@pdf_bp.route("/ocr",            methods=["POST"])
def ocr_pdf():
    return _handle("ocr_pdf", "pdf", "OCR completed successfully")

@pdf_bp.route("/compare",        methods=["POST"])
def compare_pdf():
    return _handle("compare_pdf", "zip", "PDF comparison completed",
                   multi=True, field="files")


# ── Canvas Editor (visual in-place text editing) ─────────────────────────────
# Synchronous endpoints — the user is waiting interactively and the work is
# fast (<2s for typical PDFs), so these run inline (no Celery / Redis job).
#   POST /api/pdf/parse-canvas : PDF            → page images + editable spans (JSON)
#   POST /api/pdf/save-canvas  : PDF + changes  → rebuilt PDF (binary download)
# Scanned PDFs are auto-OCR'd inside parse-canvas.

_CANVAS_MAX_UPLOAD  = 50 * 1024 * 1024   # 50 MB
_CANVAS_MAX_CHANGES = 5000
_CANVAS_MAX_TEXTLEN = 2000


def _canvas_take_upload():
    """Save the uploaded PDF to TEMP_FOLDER with a 50 MB cap.
    Returns (path, None) on success or (None, error_response)."""
    f = request.files.get("file")
    if not f or not f.filename:
        return None, Result.error("No file uploaded (field='file')", 400)
    if (request.content_length or 0) > _CANVAS_MAX_UPLOAD:
        return None, Result.error(
            "File too large — the visual editor supports files up to 50 MB. "
            "Try Compress PDF first.", 413)
    fd, path = tempfile.mkstemp(suffix=".pdf", dir=Config.TEMP_FOLDER)
    os.close(fd)
    f.seek(0)
    with open(path, "wb") as out:
        shutil.copyfileobj(f, out, length=65536)
    size = os.path.getsize(path)
    if size == 0:
        os.remove(path)
        return None, Result.error("Uploaded file is empty", 400)
    if size > _CANVAS_MAX_UPLOAD:
        os.remove(path)
        return None, Result.error(
            "File too large — the visual editor supports files up to 50 MB. "
            "Try Compress PDF first.", 413)
    return path, None


@pdf_bp.route("/parse-canvas", methods=["POST"])
def parse_canvas():
    rl = JobController.check_rate_limit(request)
    if rl:
        return rl
    path, err = _canvas_take_upload()
    if err:
        return err
    try:
        from engines.pdf_engine import _parse_canvas_sync
        return jsonify(_parse_canvas_sync(path)), 200
    except ValidationError as ex:
        return Result.error(ex.message, 400)
    except Exception as ex:
        log.exception("parse-canvas failed")
        return Result.error(f"Could not read this PDF for editing: {ex}", 500)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


@pdf_bp.route("/save-canvas", methods=["POST"])
def save_canvas():
    rl = JobController.check_rate_limit(request)
    if rl:
        return rl
    path, err = _canvas_take_upload()
    if err:
        return err
    try:
        raw = request.form.get("changes", "")
        if not raw:
            return Result.error("No changes provided", 400)
        try:
            changes = json.loads(raw)
        except (TypeError, ValueError):
            return Result.error("changes is not valid JSON", 400)
        if not isinstance(changes, list) or not changes:
            return Result.error("No changes provided", 400)
        if len(changes) > _CANVAS_MAX_CHANGES:
            return Result.error(
                f"Too many edits ({len(changes)}); limit is {_CANVAS_MAX_CHANGES}.", 400)
        for ch in changes:
            if isinstance(ch, dict) and "new_text" in ch:
                ch["new_text"] = str(ch["new_text"])[:_CANVAS_MAX_TEXTLEN]
        scanned = str(request.form.get("scanned", "")).lower() in ("1", "true", "yes")

        from engines.pdf_engine import _save_canvas_sync
        pdf_bytes = _save_canvas_sync(path, changes, scanned=scanned)
        return send_file(
            io.BytesIO(pdf_bytes),
            mimetype="application/pdf",
            as_attachment=True,
            download_name="edited.pdf",
        )
    except ValidationError as ex:
        return Result.error(ex.message, 400)
    except Exception as ex:
        log.exception("save-canvas failed")
        return Result.error(f"Could not save your edited PDF: {ex}", 500)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
