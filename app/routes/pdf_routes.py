"""
app/routes/pdf_routes.py — PDFWala

Tool endpoints are NOT hand-written anymore. They are registered in a loop from
the Single Source of Truth (catalog/manifest/pdf.yaml -> catalog.registry).
Adding a PDF tool = add one entry to the manifest, rebuild, done — no route edits.

Only the special interactive endpoints (edit-text, canvas) remain hand-written,
because they bypass the standard upload -> pipeline -> download flow.
"""

import io
import json
import logging
import os
import shutil
import tempfile

from flask import Blueprint, request, jsonify, send_file

from app.controllers.job_controller import JobController
from catalog.registry import registry
from config import Config
from core.exceptions import ValidationError
from core.result import Result
from services.file_service import file_service
from tasks.pdf_tasks import PDF_TASK_MAP

pdf_bp = Blueprint("pdf", __name__, url_prefix="/api/pdf")
log = logging.getLogger("pdfwala.routes.pdf")


# ── Shared handler ──────────────────────────────────────────────────────────────

def _handle(operation: str, output_ext: str, msg: str,
            multi: bool = False, field: str = "file", force_async: bool = False):
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
    # Guard: an async dispatch with no registered task would blow up inside
    # queue_service with an AttributeError on None. Fail cleanly instead.
    if task_fn is None and (force_async or file_service.is_async(size)):
        return Result.error(
            f"No async task is registered for '{operation}'. This operation "
            "cannot be processed right now.", 501,
        )
    return JobController.run_or_enqueue(
        ctx, size, task_fn, output_ext, msg, force_async=force_async,
    )


# ── Tool routes — generated from the manifest ────────────────────────────────────

def _register_tool_routes():
    for spec in registry.route_specs("pdf"):
        def _view(_s=spec):
            return _handle(_s["op"], _s["output_ext"], _s["success_msg"],
                           multi=_s["multi"], field=_s["field"],
                           force_async=_s["force_async"])
        endpoint = "tool_" + spec["op"]
        _view.__name__ = endpoint
        pdf_bp.add_url_rule(spec["route"], endpoint=endpoint,
                            view_func=_view, methods=["POST"])
    log.info("registered %d PDF tool routes from manifest",
             len(registry.route_specs("pdf")))


_register_tool_routes()


# ── Edit PDF (text-editor flow) ───────────────────────────────────────────────
# Round-trip a PDF through DOCX so the user can edit real text in the browser.
# Interactive (the user is waiting) so these run inline and return 200.

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


# ── Canvas Editor (visual in-place text editing) ─────────────────────────────
# Synchronous endpoints — the user waits interactively and the work is fast.
#   POST /api/pdf/parse-canvas : PDF            → page images + editable spans (JSON)
#   POST /api/pdf/save-canvas  : PDF + changes  → rebuilt PDF (binary download)

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
