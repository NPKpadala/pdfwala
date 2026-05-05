"""
tasks/pdf_tasks.py — PDFWala Enterprise V13.0
Celery tasks for PDF operations. Each task loads ctx from Redis → calls Pipeline.run().
Zero processing logic here.
"""

import logging
from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from workers.celery_app import celery_app
from core.context import JobContext
from core.pipeline import Pipeline
from core.exceptions import PDFWalaError
from services.redis_service import redis_service
from core.metrics import metrics

log = logging.getLogger("pdfwala.tasks.pdf")


class _BaseTask(Task):
    abstract = True

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        job_id = args[0] if args else task_id
        log.error(f"[{job_id}] task failure: {exc}")
        try:
            redis_service.job_update(job_id, {
                "status": "failed",
                "error":  str(exc)[:500],
            })
        except Exception:
            pass


def _run_job(job_id: str, operation_hint: str = "") -> None:
    data = redis_service.job_get(job_id)
    if not data:
        raise ValueError(f"Job {job_id} not found in Redis")
    ctx = JobContext.from_redis(data)
    import time
    t0 = time.perf_counter()
    ok = True
    try:
        Pipeline.run(ctx)
    except PDFWalaError as ex:
        ok = False
        ctx.mark_failed(ex.message)
        redis_service.job_set(job_id, ctx.to_redis())
        raise
    except Exception as ex:
        ok = False
        ctx.mark_failed(str(ex))
        redis_service.job_set(job_id, ctx.to_redis())
        raise
    finally:
        dur = (time.perf_counter() - t0) * 1000
        metrics.record(ctx.operation, dur, ok)


# ── Per-operation tasks ────────────────────────────────────────────────────────

@celery_app.task(base=_BaseTask, name="tasks.compress_pdf",  bind=True)
def task_compress_pdf(self, job_id):  _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.merge_pdf",     bind=True)
def task_merge_pdf(self, job_id):     _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.split_pdf",     bind=True)
def task_split_pdf(self, job_id):     _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.rotate_pdf",    bind=True)
def task_rotate_pdf(self, job_id):    _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.watermark_pdf", bind=True)
def task_watermark_pdf(self, job_id): _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.page_numbers",  bind=True)
def task_page_numbers(self, job_id):  _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.crop_pdf",      bind=True)
def task_crop_pdf(self, job_id):      _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.pdf_info",      bind=True)
def task_pdf_info(self, job_id):      _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.protect_pdf",   bind=True)
def task_protect_pdf(self, job_id):   _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.unlock_pdf",    bind=True)
def task_unlock_pdf(self, job_id):    _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.sign_pdf",      bind=True)
def task_sign_pdf(self, job_id):      _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.redact_pdf",    bind=True)
def task_redact_pdf(self, job_id):    _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.repair_pdf",    bind=True)
def task_repair_pdf(self, job_id):    _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.linearize_pdf", bind=True)
def task_linearize_pdf(self, job_id): _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.ocr_pdf",       bind=True)
def task_ocr_pdf(self, job_id):       _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.pdf_to_image",  bind=True)
def task_pdf_to_image(self, job_id):  _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.pdf_to_word",   bind=True)
def task_pdf_to_word(self, job_id):   _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.pdf_to_excel",  bind=True)
def task_pdf_to_excel(self, job_id):  _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.pdf_to_ppt",    bind=True)
def task_pdf_to_ppt(self, job_id):    _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.pdf_to_pdfa",   bind=True)
def task_pdf_to_pdfa(self, job_id):   _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.compare_pdf",   bind=True)
def task_compare_pdf(self, job_id):   _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.pdf_to_jpg",    bind=True)
def task_pdf_to_jpg(self, job_id):    _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.pdf_to_png",    bind=True)
def task_pdf_to_png(self, job_id):    _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.remove_pages",  bind=True)
def task_remove_pages(self, job_id):  _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.extract_pages", bind=True)
def task_extract_pages(self, job_id): _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.organize_pdf",  bind=True)
def task_organize_pdf(self, job_id):  _run_job(job_id)


# ── Operation → task function map ─────────────────────────────────────────────
PDF_TASK_MAP = {
    "compress_pdf":  task_compress_pdf,
    "merge_pdf":     task_merge_pdf,
    "split_pdf":     task_split_pdf,
    "rotate_pdf":    task_rotate_pdf,
    "watermark_pdf": task_watermark_pdf,
    "page_numbers":  task_page_numbers,
    "crop_pdf":      task_crop_pdf,
    "pdf_info":      task_pdf_info,
    "protect_pdf":   task_protect_pdf,
    "unlock_pdf":    task_unlock_pdf,
    "sign_pdf":      task_sign_pdf,
    "redact_pdf":    task_redact_pdf,
    "repair_pdf":    task_repair_pdf,
    "linearize_pdf": task_linearize_pdf,
    "ocr_pdf":       task_ocr_pdf,
    "pdf_to_image":  task_pdf_to_image,
    "pdf_to_word":   task_pdf_to_word,
    "pdf_to_excel":  task_pdf_to_excel,
    "pdf_to_ppt":    task_pdf_to_ppt,
    "pdf_to_pdfa":   task_pdf_to_pdfa,
    "compare_pdf":   task_compare_pdf,
    "pdf_to_jpg":    task_pdf_to_jpg,
    "pdf_to_png":    task_pdf_to_png,
    "remove_pages":  task_remove_pages,
    "extract_pages": task_extract_pages,
    "organize_pdf":  task_organize_pdf,
}
