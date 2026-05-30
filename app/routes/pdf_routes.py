"""
app/routes/pdf_routes.py — PDFWala Enterprise V13.0
All PDF tool endpoints. Routes ONLY: validate input, build ctx, enqueue/run.
Zero processing logic.
"""

from flask import Blueprint, request

from app.controllers.job_controller import JobController
from core.exceptions import ValidationError
from core.result import Result
from services.file_service import file_service
from tasks.pdf_tasks import PDF_TASK_MAP

pdf_bp = Blueprint("pdf", __name__, url_prefix="/api/pdf")

# ── Helper ─────────────────────────────────────────────────────────────────────

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
    return JobController.run_or_enqueue(ctx, size, task_fn, output_ext, msg)


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
