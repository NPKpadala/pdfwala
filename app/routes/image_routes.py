"""
app/routes/image_routes.py — PDFWala Enterprise V13.0
"""

from flask import Blueprint, request

from app.controllers.job_controller import JobController
from core.exceptions import ValidationError
from core.result import Result
from services.file_service import file_service
from tasks.image_tasks import IMAGE_TASK_MAP

image_bp = Blueprint("image", __name__, url_prefix="/api/image")


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
    task_fn = IMAGE_TASK_MAP.get(operation)
    return JobController.run_or_enqueue(ctx, size, task_fn, output_ext, msg)


@image_bp.route("/compress",   methods=["POST"])
def compress_image():
    return _handle("compress_image",  "jpg",  "Image compressed")

@image_bp.route("/resize",     methods=["POST"])
def resize_image():
    return _handle("resize_image",    "jpg",  "Image resized")

@image_bp.route("/convert",    methods=["POST"])
def convert_image():
    fmt = request.form.get("format", "jpg")
    return _handle("convert_image", fmt, "Image converted")

@image_bp.route("/crop",       methods=["POST"])
def crop_image():
    return _handle("crop_image",      "jpg",  "Image cropped")

@image_bp.route("/rotate",     methods=["POST"])
def rotate_image():
    return _handle("rotate_image",    "jpg",  "Image rotated")

@image_bp.route("/flip",       methods=["POST"])
def flip_image():
    return _handle("flip_image",      "jpg",  "Image flipped")

@image_bp.route("/grayscale",  methods=["POST"])
def grayscale_image():
    return _handle("grayscale_image", "jpg",  "Image converted to grayscale")

@image_bp.route("/enhance",    methods=["POST"])
def enhance_image():
    return _handle("enhance_image",   "jpg",  "Image enhanced")

@image_bp.route("/watermark",  methods=["POST"])
def watermark_image():
    return _handle("watermark_image", "jpg",  "Watermark added")

@image_bp.route("/add-text",   methods=["POST"])
def add_text_image():
    return _handle("add_text_image",  "jpg",  "Text added to image")

@image_bp.route("/to-pdf",     methods=["POST"])
def image_to_pdf():
    return _handle("image_to_pdf",    "pdf",  "Image converted to PDF")

@image_bp.route("/to-pdf/multiple", methods=["POST"])
def images_to_pdf():
    return _handle("images_to_pdf",   "pdf",  "Images merged to PDF",
                   multi=True, field="files")

@image_bp.route("/remove-bg",  methods=["POST"])
def remove_bg():
    return _handle("remove_bg",       "png",  "Background removed")

@image_bp.route("/merge",      methods=["POST"])
def merge_images():
    return _handle("merge_images",    "jpg",  "Images merged",
                   multi=True, field="files")

@image_bp.route("/png-to-jpg",   methods=["POST"])
def png_to_jpg():
    return _handle("png_to_jpg", "jpg", "PNG converted to JPG")

@image_bp.route("/webp-to-jpg",  methods=["POST"])
def webp_to_jpg():
    return _handle("webp_to_jpg", "jpg", "WebP converted to JPG")

@image_bp.route("/to-excel",   methods=["POST"])
def image_to_excel():
    return _handle("image_to_excel", "xlsx", "Image OCR → Excel")

@image_bp.route("/to-word",    methods=["POST"])
def image_to_word():
    return _handle("image_to_word",  "docx", "Image OCR → Word")
