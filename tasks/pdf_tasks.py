"""
tasks/pdf_tasks.py — Celery tasks for PDF operations.

Tasks are NO LONGER hand-written. One task per PDF tool is generated from the
Single Source of Truth (catalog.registry) with the exact same names as before
("tasks.<op>"), and PDF_TASK_MAP is built from the same loop. Adding a tool =
add a manifest entry; its task appears automatically. No second registry.
"""
import logging
import time

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded

from catalog.registry import registry
from core.context import JobContext
from core.exceptions import PDFWalaError
from core.metrics import metrics
from core.pipeline import Pipeline
from services.redis_service import redis_service
from workers.celery_app import celery_app

log = logging.getLogger("pdfwala.tasks.pdf")


class _BaseTask(Task):
    abstract = True

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        job_id = args[0] if args else task_id
        log.error(f"[{job_id}] task failure: {exc}")
        try:
            # _run_job already writes a friendly, operation-specific error before
            # re-raising. Only fill in a generic message if the job hasn't been
            # marked failed yet, so we don't clobber the better message.
            data = redis_service.job_get(job_id)
            if not data or data.get("status") != "failed":
                redis_service.job_update(job_id, {
                    "status": "failed",
                    "error":  str(exc)[:500],
                })
        except Exception:
            pass


def _run_job(job_id: str) -> None:
    data = redis_service.job_get(job_id)
    if not data:
        raise ValueError(f"Job {job_id} not found in Redis")
    ctx = JobContext.from_redis(data)
    t0 = time.perf_counter()
    ok = True
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
        dur = (time.perf_counter() - t0) * 1000
        metrics.record(ctx.operation, dur, ok)


def _make_task(op: str):
    """Create one Celery task named 'tasks.<op>' (same names as the old
    hand-written tasks, so in-flight jobs and worker routing are unaffected)."""
    @celery_app.task(base=_BaseTask, name="tasks." + op, bind=True)
    def _task(self, job_id):
        _run_job(job_id)
    return _task


# ── Generated task registry (single source of truth: the manifest) ──────────────
PDF_TASK_MAP = {t["engine"]: _make_task(t["engine"]) for t in registry.module("pdf")}

log.info("registered %d PDF Celery tasks from manifest", len(PDF_TASK_MAP))
