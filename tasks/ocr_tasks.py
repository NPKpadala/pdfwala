"""
PDFWala Enterprise V11.0.0
tasks/ocr_tasks.py — Async OCR Celery task with per-page progress reporting.

Fixes vs V10:
  - time_limit=3600 / soft_time_limit=3300 (60 / 55 min — OCR is slow)
  - conf value int-conversion wrapped in try/except (matches app.py HIGH-01 fix)
  - pix freed immediately after tobytes() to release C-heap (HIGH-07)
  - img.close() called after OCR to release PIL memory (HIGH-07)
  - None-guard on celery_app removed (always created in workers/celery_app.py)
"""

import io
import os
import logging

from workers.celery_app import celery_app
from services.redis_service import redis_service
from utils.helpers import get_timestamp

log = logging.getLogger("pdfwala.tasks.ocr")

try:
    import fitz
    FITZ_AVAILABLE = True
except ImportError:
    FITZ_AVAILABLE = False

try:
    import pytesseract
    from pytesseract import Output as TesseractOutput
    from PIL import Image
    TESSERACT_AVAILABLE = True
except ImportError:
    TESSERACT_AVAILABLE = False


@celery_app.task(
    bind=True,
    max_retries=2,
    name="pdfwala.tasks.ocr_tasks.ocr_pdf_task",
    queue="slow",
    time_limit=3600,       # 60 min hard kill — OCR is inherently slow
    soft_time_limit=3300,  # 55 min soft limit
)
def ocr_pdf_task(
    self,
    input_path: str,
    output_path: str,
    job_id: str,
    lang: str = "eng",
    dpi: int = 300,
    psm: int = 3,
    oem: int = 3,
):
    """
    Async OCR: rasterise each page, run Tesseract, overlay invisible text.
    Reports progress per page via Redis job store.
    Cleans up input_path in finally block.
    """
    try:
        if not TESSERACT_AVAILABLE:
            raise RuntimeError("pytesseract not installed")
        if not FITZ_AVAILABLE:
            raise RuntimeError("PyMuPDF not installed")

        redis_service.job_update(job_id, {"status": "processing"})

        src_doc = fitz.open(input_path)
        out_doc = fitz.open()
        total   = len(src_doc)
        redis_service.job_update(job_id, {"total_pages": str(total)})

        try:
            for page_num, src_page in enumerate(src_doc):
                pw, ph = src_page.rect.width, src_page.rect.height

                if src_page.get_text().strip():
                    # Page already has text — copy as-is
                    new_page = out_doc.new_page(width=pw, height=ph)
                    new_page.show_pdf_page(
                        fitz.Rect(0, 0, pw, ph), src_doc, page_num
                    )
                else:
                    mat = fitz.Matrix(dpi / 72, dpi / 72)
                    pix = src_page.get_pixmap(
                        matrix=mat, alpha=False, colorspace=fitz.csGRAY
                    )
                    # FIX HIGH-07: free C-heap pixmap immediately after tobytes()
                    img_bytes = pix.tobytes("png")
                    img_sx    = pw / pix.width
                    img_sy    = ph / pix.height
                    pix       = None  # release C-heap

                    tess_cfg = f"--psm {psm} --oem {oem}"
                    img      = Image.open(io.BytesIO(img_bytes))
                    try:
                        ocr_data = pytesseract.image_to_data(
                            img,
                            lang=lang,
                            output_type=TesseractOutput.DICT,
                            config=tess_cfg,
                        )
                    finally:
                        img.close()  # FIX HIGH-07: release PIL memory

                    new_page = out_doc.new_page(width=pw, height=ph)
                    new_page.show_pdf_page(
                        fitz.Rect(0, 0, pw, ph), src_doc, page_num
                    )

                    # FIX HIGH-01: use zip() for ragged-dict safety + safe int cast
                    for word_str, conf_str, x0, y0, wd, ht in zip(
                        ocr_data.get("text",   []),
                        ocr_data.get("conf",   []),
                        ocr_data.get("left",   []),
                        ocr_data.get("top",    []),
                        ocr_data.get("width",  []),
                        ocr_data.get("height", []),
                    ):
                        word = (word_str or "").strip()
                        try:
                            conf = int(conf_str)
                        except (ValueError, TypeError):
                            conf = 0
                        if not word or conf < 30:
                            continue
                        x0_f = float(x0) * img_sx
                        y1_f = (float(y0) + float(ht)) * img_sy
                        fs   = max(4.0, float(ht) * img_sy * 0.85)
                        new_page.insert_text(
                            (x0_f, y1_f - 1),
                            word + " ",
                            fontsize=fs,
                            fontname="helv",
                            color=(0, 0, 0),
                            render_mode=3,
                            overlay=True,
                        )

                pct = int((page_num + 1) / total * 100)
                redis_service.job_update(job_id, {
                    "progress":     str(pct),
                    "current_page": str(page_num + 1),
                })

            out_doc.save(output_path, deflate=True, garbage=2)

        finally:
            out_doc.close()
            src_doc.close()

        redis_service.job_update(job_id, {
            "status":       "completed",
            "progress":     "100",
            "output_path":  output_path,
            "completed_at": get_timestamp(),
        })
        return {"status": "completed", "output": output_path}

    except Exception as ex:
        log.error(f"ocr_pdf_task {job_id}: {ex}")
        redis_service.job_update(job_id, {"status": "failed", "error": str(ex)})
        raise self.retry(exc=ex, countdown=30)
    finally:
        try:
            os.remove(input_path)
        except OSError:
            pass
