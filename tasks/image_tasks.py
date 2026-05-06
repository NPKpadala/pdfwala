"""
tasks/image_tasks.py — PDFWala Enterprise V14.0

V14 FIX: SoftTimeLimitExceeded now caught and job marked as failed in Redis.
"""

import logging
import time

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from workers.celery_app import celery_app
from core.context import JobContext
from core.pipeline import Pipeline
from core.exceptions import PDFWalaError
from services.redis_service import redis_service
from core.metrics import metrics

log = logging.getLogger("pdfwala.tasks.image")


class _BaseTask(Task):
    abstract = True

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        job_id = args[0] if args else task_id
        try:
            redis_service.job_update(job_id, {"status": "failed", "error": str(exc)[:500]})
        except Exception:
            pass


def _run_job(job_id: str) -> None:
    data = redis_service.job_get(job_id)
    if not data:
        raise ValueError(f"Job {job_id} not found in Redis")
    ctx = JobContext.from_redis(data)
    t0  = time.perf_counter()
    ok  = True
    try:
        Pipeline.run(ctx)
    except SoftTimeLimitExceeded:
        ok = False
        ctx.mark_failed("Task exceeded time limit and was terminated")
        redis_service.job_set(job_id, ctx.to_redis())
        raise
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
        metrics.record(ctx.operation, (time.perf_counter() - t0) * 1000, ok)


@celery_app.task(base=_BaseTask, name="tasks.compress_image",  bind=True)
def task_compress_image(self, job_id):  _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.resize_image",    bind=True)
def task_resize_image(self, job_id):    _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.convert_image",   bind=True)
def task_convert_image(self, job_id):   _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.crop_image",      bind=True)
def task_crop_image(self, job_id):      _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.rotate_image",    bind=True)
def task_rotate_image(self, job_id):    _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.watermark_image", bind=True)
def task_watermark_image(self, job_id): _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.image_to_pdf",    bind=True)
def task_image_to_pdf(self, job_id):    _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.images_to_pdf",   bind=True)
def task_images_to_pdf(self, job_id):   _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.remove_bg",       bind=True)
def task_remove_bg(self, job_id):       _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.enhance_image",   bind=True)
def task_enhance_image(self, job_id):   _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.grayscale_image", bind=True)
def task_grayscale_image(self, job_id): _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.flip_image",      bind=True)
def task_flip_image(self, job_id):      _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.add_text_image",  bind=True)
def task_add_text_image(self, job_id):  _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.merge_images",    bind=True)
def task_merge_images(self, job_id):    _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.png_to_jpg",      bind=True)
def task_png_to_jpg(self, job_id):      _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.webp_to_jpg",     bind=True)
def task_webp_to_jpg(self, job_id):     _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.image_to_excel",  bind=True)
def task_image_to_excel(self, job_id):  _run_job(job_id)

@celery_app.task(base=_BaseTask, name="tasks.image_to_word",   bind=True)
def task_image_to_word(self, job_id):   _run_job(job_id)


IMAGE_TASK_MAP = {
    "compress_image":  task_compress_image,
    "resize_image":    task_resize_image,
    "convert_image":   task_convert_image,
    "crop_image":      task_crop_image,
    "rotate_image":    task_rotate_image,
    "watermark_image": task_watermark_image,
    "image_to_pdf":    task_image_to_pdf,
    "images_to_pdf":   task_images_to_pdf,
    "remove_bg":       task_remove_bg,
    "enhance_image":   task_enhance_image,
    "grayscale_image": task_grayscale_image,
    "flip_image":      task_flip_image,
    "add_text_image":  task_add_text_image,
    "merge_images":    task_merge_images,
    "png_to_jpg":      task_png_to_jpg,
    "webp_to_jpg":     task_webp_to_jpg,
    "image_to_excel":  task_image_to_excel,
    "image_to_word":   task_image_to_word,
}
