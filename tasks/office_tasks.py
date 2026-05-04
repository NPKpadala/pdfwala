"""
tasks/office_tasks.py — PDFWala Enterprise V13.0
"""

import logging
from celery import Task

from workers.celery_app import celery_app
from core.context import JobContext
from core.pipeline import Pipeline
from core.exceptions import PDFWalaError
from services.redis_service import redis_service
from core.metrics import metrics

log = logging.getLogger("pdfwala.tasks.office")


class _BaseTask(Task):
    abstract = True

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        job_id = args[0] if args else task_id
        try:
            redis_service.job_update(job_id, {"status": "failed", "error": str(exc)[:500]})
        except Exception:
            pass


def _run_job(job_id: str) -> None:
    import time
    data = redis_service.job_get(job_id)
    if not data:
        raise ValueError(f"Job {job_id} not found in Redis")
    ctx = JobContext.from_redis(data)
    t0 = time.perf_counter()
    ok = True
    try:
        Pipeline.run(ctx)
    except (PDFWalaError, Exception) as ex:
        ok = False
        ctx.mark_failed(str(ex))
        redis_service.job_set(job_id, ctx.to_redis())
        raise
    finally:
        metrics.record(ctx.operation, (time.perf_counter() - t0) * 1000, ok)


@celery_app.task(base=_BaseTask, name="tasks.word_to_pdf",    bind=True)
def task_word_to_pdf(self, job_id):    _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.word_to_txt",    bind=True)
def task_word_to_txt(self, job_id):    _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.word_to_html",   bind=True)
def task_word_to_html(self, job_id):   _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.word_to_json",   bind=True)
def task_word_to_json(self, job_id):   _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.word_to_excel",  bind=True)
def task_word_to_excel(self, job_id):  _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.word_to_ppt",    bind=True)
def task_word_to_ppt(self, job_id):    _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.word_to_jpg",    bind=True)
def task_word_to_jpg(self, job_id):    _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.word_to_png",    bind=True)
def task_word_to_png(self, job_id):    _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.edit_word",      bind=True)
def task_edit_word(self, job_id):      _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.compress_word",  bind=True)
def task_compress_word(self, job_id):  _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.unlock_word",    bind=True)
def task_unlock_word(self, job_id):    _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.protect_word",   bind=True)
def task_protect_word(self, job_id):   _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.excel_to_pdf",   bind=True)
def task_excel_to_pdf(self, job_id):   _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.excel_to_csv",   bind=True)
def task_excel_to_csv(self, job_id):   _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.excel_to_word",  bind=True)
def task_excel_to_word(self, job_id):  _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.excel_to_json",  bind=True)
def task_excel_to_json(self, job_id):  _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.compress_excel", bind=True)
def task_compress_excel(self, job_id): _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.unlock_excel",   bind=True)
def task_unlock_excel(self, job_id):   _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.protect_excel",  bind=True)
def task_protect_excel(self, job_id):  _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.excel_to_jpg",   bind=True)
def task_excel_to_jpg(self, job_id):   _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.excel_to_ppt",   bind=True)
def task_excel_to_ppt(self, job_id):   _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.repair_excel",   bind=True)
def task_repair_excel(self, job_id):   _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.ppt_to_pdf",     bind=True)
def task_ppt_to_pdf(self, job_id):     _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.ppt_to_jpg",     bind=True)
def task_ppt_to_jpg(self, job_id):     _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.compress_ppt",   bind=True)
def task_compress_ppt(self, job_id):   _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.unlock_ppt",     bind=True)
def task_unlock_ppt(self, job_id):     _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.protect_ppt",    bind=True)
def task_protect_ppt(self, job_id):    _run_job(job_id)


OFFICE_TASK_MAP = {
    "word_to_pdf":    task_word_to_pdf,
    "word_to_txt":    task_word_to_txt,
    "word_to_html":   task_word_to_html,
    "word_to_json":   task_word_to_json,
    "word_to_excel":  task_word_to_excel,
    "word_to_ppt":    task_word_to_ppt,
    "word_to_jpg":    task_word_to_jpg,
    "word_to_png":    task_word_to_png,
    "edit_word":      task_edit_word,
    "compress_word":  task_compress_word,
    "unlock_word":    task_unlock_word,
    "protect_word":   task_protect_word,
    "excel_to_pdf":   task_excel_to_pdf,
    "excel_to_csv":   task_excel_to_csv,
    "excel_to_word":  task_excel_to_word,
    "excel_to_json":  task_excel_to_json,
    "compress_excel": task_compress_excel,
    "unlock_excel":   task_unlock_excel,
    "protect_excel":  task_protect_excel,
    "excel_to_jpg":   task_excel_to_jpg,
    "excel_to_ppt":   task_excel_to_ppt,
    "repair_excel":   task_repair_excel,
    "ppt_to_pdf":     task_ppt_to_pdf,
    "ppt_to_jpg":     task_ppt_to_jpg,
    "compress_ppt":   task_compress_ppt,
    "unlock_ppt":     task_unlock_ppt,
    "protect_ppt":    task_protect_ppt,
}
