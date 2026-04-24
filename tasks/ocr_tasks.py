"""
PDFWala Enterprise V11.1.0
tasks/ocr_tasks.py — Async OCR Celery task with chunked parallel processing.

Changes vs V11.0:
  - Chunks large PDFs via chunked_pdf_processor() from utils/pdf_utils
  - OCR_CHUNK_THRESHOLD (default 50): pages below this process single-pass
  - OCR_CHUNK_PAGES    (default 30):  pages per chunk
  - OCR_MAX_WORKERS    (default 2):   Tesseract is RAM-heavy (200 MB/worker)
  - Per-chunk retry (1x) before job-level single-pass fallback
  - Disk-space pre-check inside chunked_pdf_processor
  - Temp cleanup guaranteed even on crash
  - time_limit=3600 / soft_time_limit=3300 retained
"""

import io
import os
import logging
import tempfile

from workers.celery_app import celery_app
from services.redis_service import redis_service
from utils.helpers import get_timestamp
from utils.pdf_utils import chunked_pdf_processor, merge_pdf_chunks
from config import Config

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


# ── Config ─────────────────────────────────────────────────────────────────────

OCR_CHUNK_THRESHOLD = int(getattr(Config, "OCR_CHUNK_THRESHOLD", 50))
OCR_CHUNK_PAGES     = int(getattr(Config, "OCR_CHUNK_PAGES",     30))
OCR_MAX_WORKERS     = int(getattr(Config, "OCR_MAX_WORKERS",      2))  # RAM: ~200 MB each


# ── Per-page OCR helper (used by both paths) ───────────────────────────────────

def _ocr_single_pdf(
    input_path: str,
    output_path: str,
    lang: str,
    dpi: int,
    psm: int,
    oem: int,
    job_id: str = "",
    progress_callback=None,
) -> None:
    """
    OCR every page in input_path, write searchable PDF to output_path.
    progress_callback(page_num, total) is called after each page when provided.
    """
    if not TESSERACT_AVAILABLE:
        raise RuntimeError("pytesseract not installed")
    if not FITZ_AVAILABLE:
        raise RuntimeError("PyMuPDF not installed")

    src_doc = fitz.open(input_path)
    out_doc = fitz.open()
    total   = len(src_doc)

    try:
        for page_num, src_page in enumerate(src_doc):
            pw, ph = src_page.rect.width, src_page.rect.height

            if src_page.get_text().strip():
                # Page already has selectable text — copy as-is
                new_page = out_doc.new_page(width=pw, height=ph)
                new_page.show_pdf_page(
                    fitz.Rect(0, 0, pw, ph), src_doc, page_num
                )
            else:
                mat      = fitz.Matrix(dpi / 72, dpi / 72)
                pix      = src_page.get_pixmap(matrix=mat, alpha=False, colorspace=fitz.csGRAY)
                img_bytes = pix.tobytes("png")
                img_sx    = pw / pix.width
                img_sy    = ph / pix.height
                pix       = None  # release C-heap immediately

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
                    img.close()

                new_page = out_doc.new_page(width=pw, height=ph)
                new_page.show_pdf_page(
                    fitz.Rect(0, 0, pw, ph), src_doc, page_num
                )

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

            if progress_callback:
                progress_callback(page_num, total)

        out_doc.save(output_path, deflate=True, garbage=2)
    finally:
        out_doc.close()
        src_doc.close()


# ── Celery task ────────────────────────────────────────────────────────────────

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

    For PDFs > OCR_CHUNK_THRESHOLD pages, processes in parallel chunks via
    chunked_pdf_processor(). Falls back to single-pass if chunking fails.

    Reports progress per page (single-pass) or per chunk (chunked).
    Cleans up input_path in finally block.
    """
    try:
        if not TESSERACT_AVAILABLE:
            raise RuntimeError("pytesseract not installed")
        if not FITZ_AVAILABLE:
            raise RuntimeError("PyMuPDF not installed")

        redis_service.job_update(job_id, {"status": "processing"})

        # Count pages
        doc_check   = fitz.open(input_path)
        total_pages = len(doc_check)
        doc_check.close()
        redis_service.job_update(job_id, {"total_pages": str(total_pages)})

        if total_pages > OCR_CHUNK_THRESHOLD:
            # ── CHUNKED PATH ──────────────────────────────────────────────────
            log.info(
                f"ocr_pdf_task {job_id}: {total_pages} pages — using chunked "
                f"processing (chunk={OCR_CHUNK_PAGES}, workers={OCR_MAX_WORKERS})"
            )

            def process_ocr_chunk(
                chunk_path: str,
                chunk_idx: int,
                start_page: int,
                end_page: int,
            ) -> str:
                chunk_out = chunk_path.replace("_in.pdf", "_out.pdf")
                _ocr_single_pdf(
                    chunk_path, chunk_out, lang, dpi, psm, oem, job_id
                )
                return chunk_out

            success = chunked_pdf_processor(
                input_path=input_path,
                output_path=output_path,
                job_id=job_id,
                total_pages=total_pages,
                chunk_size=OCR_CHUNK_PAGES,
                max_workers=OCR_MAX_WORKERS,
                process_chunk_func=process_ocr_chunk,
                merge_func=merge_pdf_chunks,
                redis_service=redis_service,
                tool_name="OCR",
                report_progress=True,
                chunk_retry=1,
            )

            if not success:
                log.warning(
                    f"ocr_pdf_task {job_id}: chunked path failed — "
                    "falling back to single-pass (will be slow)"
                )
                # ── SINGLE-PASS FALLBACK ──────────────────────────────────────
                _ocr_single_pdf(input_path, output_path, lang, dpi, psm, oem, job_id)

        else:
            # ── SINGLE-PASS (small file fast path) ────────────────────────────
            log.info(f"ocr_pdf_task {job_id}: {total_pages} pages — single-pass")

            pct_cache = [0]

            def _progress(page_num: int, total: int) -> None:
                pct = int((page_num + 1) / total * 100)
                if pct != pct_cache[0]:
                    pct_cache[0] = pct
                    redis_service.job_update(job_id, {
                        "progress":     str(pct),
                        "current_page": str(page_num + 1),
                    })

            _ocr_single_pdf(
                input_path, output_path, lang, dpi, psm, oem, job_id,
                progress_callback=_progress,
            )

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise RuntimeError("Output file missing or empty after OCR")

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
