"""
services/queue_service.py — PDFWala Enterprise V13.0
Routes operations to the correct Celery queue.
"""

import logging
from typing import Callable
from config import Config
from core.context import JobContext

log = logging.getLogger("pdfwala.queue")

_PDF_FAST_OPS = {
    "compress_pdf", "rotate_pdf", "watermark_pdf", "page_numbers",
    "crop_pdf", "protect_pdf", "unlock_pdf", "sign_pdf", "redact_pdf",
    "pdf_info", "remove_pages", "extract_pages", "organize_pdf",
    "split_pdf", "linearize_pdf", "repair_pdf", "edit_pdf",
}
_PDF_SLOW_OPS = {
    "ocr_pdf", "compare_pdf", "merge_pdf",
    "pdf_to_image", "pdf_to_jpg", "pdf_to_png",
    "pdf_to_word", "pdf_to_excel", "pdf_to_ppt", "pdf_to_pdfa",
}
_OFFICE_OPS = {
    "word_to_pdf", "word_to_txt", "word_to_html", "word_to_json",
    "word_to_excel", "word_to_ppt", "word_to_jpg", "word_to_png",
    "edit_word", "compress_word", "unlock_word", "protect_word",
    "excel_to_pdf", "excel_to_csv", "excel_to_word", "excel_to_json",
    "compress_excel", "unlock_excel", "protect_excel",
    "excel_to_jpg", "excel_to_ppt", "repair_excel",
    "ppt_to_pdf", "ppt_to_jpg", "compress_ppt",
    "unlock_ppt", "protect_ppt",
    "excel_to_html", "excel_to_png", "html_to_pdf",
}
_IMAGE_OPS = {
    "compress_image", "resize_image", "convert_image",
    "crop_image", "rotate_image", "watermark_image",
    "image_to_pdf", "images_to_pdf", "remove_bg",
    "enhance_image", "grayscale_image", "flip_image",
    "add_text_image", "merge_images",
    "png_to_jpg", "webp_to_jpg",
}
# OCR-heavy image ops that must run on QUEUE_SLOW
_IMAGE_SLOW_OPS = {
    "image_to_excel", "image_to_word",
}


def get_queue_for(operation: str) -> str:
    if operation in _OFFICE_OPS:
        return Config.QUEUE_OFFICE
    if operation in _PDF_SLOW_OPS:
        return Config.QUEUE_SLOW
    if operation in _IMAGE_SLOW_OPS:
        return Config.QUEUE_SLOW
    # FIX: IMAGE_OPS were previously sent to QUEUE_SLOW alongside PDF heavy ops.
    # Image ops (compress/resize/convert/crop etc.) are fast and should use QUEUE_FAST.
    if operation in _IMAGE_OPS:
        return Config.QUEUE_FAST
    return Config.QUEUE_FAST


class QueueService:

    def dispatch(self, ctx: JobContext, task_fn: Callable) -> str:
        queue  = get_queue_for(ctx.operation)
        log.info(f"[{ctx.job_id}] dispatch op={ctx.operation} queue={queue}")
        result = task_fn.apply_async(
            args=[ctx.job_id],
            queue=queue,
            task_id=ctx.job_id,
        )
        ctx.task_id  = result.id
        ctx.is_async = True
        return result.id


queue_service = QueueService()
