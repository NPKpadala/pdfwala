"""
app/routes/alias_routes.py — flat URL aliases used by the frontend.

The frontend (static/index.html) posts to flat paths like /api/compress,
/api/pdf-to-word, /api/word-to-pdf, etc., but blueprints register them under
/api/pdf/..., /api/office/..., /api/image/... — every upload was returning 404.

This module re-exposes every handler under the flat path the UI expects.
Zero processing logic — pure URL aliasing.
"""

from flask import Blueprint

from app.routes.pdf_routes    import (
    merge_pdf, split_pdf, organize_pdf, remove_pages, extract_pages,
    compress_pdf, repair_pdf, linearize_pdf,
    rotate_pdf, watermark_pdf, page_numbers, crop_pdf, redact_pdf,
    protect_pdf, unlock_pdf, sign_pdf,
    pdf_info,
    pdf_to_image, pdf_to_jpg, pdf_to_png,
    pdf_to_word, pdf_to_excel, pdf_to_ppt, pdf_to_pdfa,
    ocr_pdf, compare_pdf,
)
from app.routes.office_routes import (
    word_to_pdf, word_to_txt, word_to_html, word_to_json,
    word_to_excel, word_to_ppt, word_to_jpg, word_to_png,
    edit_word, compress_word, unlock_word, protect_word,
    excel_to_pdf, excel_to_csv, excel_to_word, excel_to_json,
    compress_excel, unlock_excel, protect_excel,
    excel_to_jpg, excel_to_ppt, repair_excel,
    excel_to_html, excel_to_png,
    ppt_to_pdf, ppt_to_jpg, compress_ppt, unlock_ppt, protect_ppt,
    html_to_pdf,
)
from app.routes.image_routes  import (
    compress_image, resize_image, convert_image, crop_image,
    rotate_image, flip_image, grayscale_image, enhance_image,
    watermark_image, add_text_image,
    image_to_pdf, images_to_pdf, remove_bg, merge_images,
    png_to_jpg, webp_to_jpg, image_to_excel, image_to_word,
)


alias_bp = Blueprint("alias", __name__, url_prefix="/api")


def _add(rule: str, view, methods=("POST",), endpoint: str = None):
    alias_bp.add_url_rule(
        rule,
        endpoint=endpoint or f"alias_{view.__name__}",
        view_func=view,
        methods=list(methods),
    )


# ── PDF (flat) ────────────────────────────────────────────────────────────────
_add("/merge",           merge_pdf)
_add("/split",           split_pdf)
_add("/organize",        organize_pdf)
_add("/remove-pages",    remove_pages)
_add("/extract-pages",   extract_pages)
_add("/compress",        compress_pdf)
_add("/repair-pdf",      repair_pdf)
_add("/linearize",       linearize_pdf)
_add("/rotate",          rotate_pdf)
_add("/watermark",       watermark_pdf)
_add("/page-numbers",    page_numbers)
_add("/crop",            crop_pdf)
_add("/redact-pdf",      redact_pdf)
_add("/protect",         protect_pdf)
_add("/unlock",          unlock_pdf)
_add("/sign-pdf",        sign_pdf)
_add("/info",            pdf_info)
_add("/pdf-to-image",    pdf_to_image)
_add("/pdf-to-jpg",      pdf_to_jpg)
_add("/pdf-to-png",      pdf_to_png)
_add("/pdf-to-word",     pdf_to_word)
_add("/pdf-to-excel",    pdf_to_excel)
_add("/pdf-to-ppt",      pdf_to_ppt)
_add("/pdf-to-pdfa",     pdf_to_pdfa)
_add("/ocr-pdf",         ocr_pdf)
_add("/compare-pdf",     compare_pdf)

# ── Office (flat) ─────────────────────────────────────────────────────────────
_add("/word-to-pdf",     word_to_pdf)
_add("/word-to-txt",     word_to_txt)
_add("/word-to-html",    word_to_html)
_add("/word-to-json",    word_to_json)
_add("/word-to-excel",   word_to_excel)
_add("/word-to-ppt",     word_to_ppt)
_add("/word-to-jpg",     word_to_jpg)
_add("/word-to-png",     word_to_png)
_add("/edit-word",       edit_word)
_add("/compress-word",   compress_word)
_add("/unlock-word",     unlock_word)
_add("/protect-word",    protect_word)

_add("/excel-to-pdf",    excel_to_pdf)
_add("/excel-to-csv",    excel_to_csv)
_add("/excel-to-word",   excel_to_word)
_add("/excel-to-json",   excel_to_json)
_add("/excel-to-jpg",    excel_to_jpg)
_add("/excel-to-ppt",    excel_to_ppt)
_add("/excel-to-html",   excel_to_html)
_add("/excel-to-png",    excel_to_png)
_add("/compress-excel",  compress_excel)
_add("/unlock-excel",    unlock_excel)
_add("/protect-excel",   protect_excel)
_add("/repair-excel",    repair_excel)

_add("/ppt-to-pdf",      ppt_to_pdf)
_add("/ppt-to-jpg",      ppt_to_jpg)
_add("/compress-ppt",    compress_ppt)
_add("/unlock-ppt",      unlock_ppt)
_add("/protect-ppt",     protect_ppt)

_add("/html-to-pdf",     html_to_pdf)

# ── Image (flat) ──────────────────────────────────────────────────────────────
_add("/compress-image",  compress_image)
_add("/resize-image",    resize_image)
_add("/convert-image",   convert_image)
_add("/crop-image",      crop_image)
_add("/rotate-image",    rotate_image)
_add("/flip-image",      flip_image)
_add("/grayscale-image", grayscale_image)
_add("/enhance-image",   enhance_image)
_add("/watermark-image", watermark_image)
_add("/add-text-image",  add_text_image)
_add("/image-to-pdf",    image_to_pdf)
_add("/jpg-to-pdf",      image_to_pdf, endpoint="alias_jpg_to_pdf")
_add("/images-to-pdf",   images_to_pdf)
_add("/remove-bg",       remove_bg)
_add("/merge-images",    merge_images)
_add("/png-to-jpg",      png_to_jpg)
_add("/webp-to-jpg",     webp_to_jpg)
_add("/image-to-excel",  image_to_excel)
_add("/image-to-word",   image_to_word)
