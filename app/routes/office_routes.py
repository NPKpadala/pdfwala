"""
app/routes/office_routes.py — PDFWala Enterprise V13.0
"""

from flask import Blueprint, request

from app.controllers.job_controller import JobController
from core.exceptions import ValidationError
from core.result import Result
from services.file_service import file_service
from tasks.office_tasks import OFFICE_TASK_MAP

office_bp = Blueprint("office", __name__, url_prefix="/api/office")


def _handle(operation: str, output_ext: str, msg: str, field: str = "file"):
    rl = JobController.check_rate_limit(request)
    if rl:
        return rl
    ctx = JobController.build_ctx(request, operation)
    try:
        size = file_service.save_single(request, ctx, field)
    except ValidationError as ex:
        return Result.error(ex.message, 400)
    task_fn = OFFICE_TASK_MAP.get(operation)
    return JobController.run_or_enqueue(ctx, size, task_fn, output_ext, msg)


# ── Word ───────────────────────────────────────────────────────────────────────

@office_bp.route("/word/to-pdf",    methods=["POST"])
def word_to_pdf():
    return _handle("word_to_pdf",   "pdf",  "Word converted to PDF")

@office_bp.route("/word/to-txt",    methods=["POST"])
def word_to_txt():
    return _handle("word_to_txt",   "txt",  "Word converted to text")

@office_bp.route("/word/to-html",   methods=["POST"])
def word_to_html():
    return _handle("word_to_html",  "html", "Word converted to HTML")

@office_bp.route("/word/to-json",   methods=["POST"])
def word_to_json():
    return _handle("word_to_json",  "json", "Word converted to JSON")

@office_bp.route("/word/to-excel",  methods=["POST"])
def word_to_excel():
    return _handle("word_to_excel", "xlsx", "Word converted to Excel")

@office_bp.route("/word/to-ppt",    methods=["POST"])
def word_to_ppt():
    return _handle("word_to_ppt",   "pptx", "Word converted to PowerPoint")

@office_bp.route("/word/to-jpg",    methods=["POST"])
def word_to_jpg():
    return _handle("word_to_jpg",   "zip",  "Word converted to JPG")

@office_bp.route("/word/to-png",    methods=["POST"])
def word_to_png():
    return _handle("word_to_png",   "zip",  "Word converted to PNG")

@office_bp.route("/word/edit",      methods=["POST"])
def edit_word():
    return _handle("edit_word",     "docx", "Word document edited")

@office_bp.route("/word/compress",  methods=["POST"])
def compress_word():
    return _handle("compress_word", "docx", "Word compressed")

@office_bp.route("/word/unlock",    methods=["POST"])
def unlock_word():
    return _handle("unlock_word",   "docx", "Word unlocked")

@office_bp.route("/word/protect",   methods=["POST"])
def protect_word():
    return _handle("protect_word",  "docx", "Word protected")


# ── Excel ──────────────────────────────────────────────────────────────────────

@office_bp.route("/excel/to-pdf",   methods=["POST"])
def excel_to_pdf():
    return _handle("excel_to_pdf",   "pdf",  "Excel converted to PDF")

@office_bp.route("/excel/to-csv",   methods=["POST"])
def excel_to_csv():
    return _handle("excel_to_csv",   "csv",  "Excel converted to CSV")

@office_bp.route("/excel/to-word",  methods=["POST"])
def excel_to_word():
    return _handle("excel_to_word",  "docx", "Excel converted to Word")

@office_bp.route("/excel/to-json",  methods=["POST"])
def excel_to_json():
    return _handle("excel_to_json",  "json", "Excel converted to JSON")

@office_bp.route("/excel/compress", methods=["POST"])
def compress_excel():
    return _handle("compress_excel", "xlsx", "Excel compressed")

@office_bp.route("/excel/unlock",   methods=["POST"])
def unlock_excel():
    return _handle("unlock_excel",   "xlsx", "Excel unlocked")

@office_bp.route("/excel/protect",  methods=["POST"])
def protect_excel():
    return _handle("protect_excel",  "xlsx", "Excel protected")

@office_bp.route("/excel/to-jpg",   methods=["POST"])
def excel_to_jpg():
    return _handle("excel_to_jpg",   "zip",  "Excel converted to JPG")

@office_bp.route("/excel/to-ppt",   methods=["POST"])
def excel_to_ppt():
    return _handle("excel_to_ppt",   "pptx", "Excel converted to PowerPoint")

@office_bp.route("/excel/repair",   methods=["POST"])
def repair_excel():
    return _handle("repair_excel",   "xlsx", "Excel repaired")


# ── PowerPoint ─────────────────────────────────────────────────────────────────

@office_bp.route("/ppt/to-pdf",     methods=["POST"])
def ppt_to_pdf():
    return _handle("ppt_to_pdf",    "pdf",  "PowerPoint converted to PDF")

@office_bp.route("/ppt/to-jpg",     methods=["POST"])
def ppt_to_jpg():
    return _handle("ppt_to_jpg",    "zip",  "PowerPoint converted to JPG")

@office_bp.route("/ppt/compress",   methods=["POST"])
def compress_ppt():
    return _handle("compress_ppt",  "pptx", "PowerPoint compressed")

@office_bp.route("/ppt/unlock",     methods=["POST"])
def unlock_ppt():
    return _handle("unlock_ppt",    "pptx", "PowerPoint unlocked")

@office_bp.route("/ppt/protect",    methods=["POST"])
def protect_ppt():
    return _handle("protect_ppt",   "pptx", "PowerPoint protected")

@office_bp.route("/excel/to-html",   methods=["POST"])
def excel_to_html():
    return _handle("excel_to_html",  "html", "Excel converted to HTML")

@office_bp.route("/excel/to-png",    methods=["POST"])
def excel_to_png():
    return _handle("excel_to_png",   "zip",  "Excel converted to PNG")

@office_bp.route("/html/to-pdf",   methods=["POST"])
def html_to_pdf():
    return _handle("html_to_pdf",  "pdf",  "HTML converted to PDF")
