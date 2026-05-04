"""
app/controllers/job_controller.py — PDFWala Enterprise V13.0
Shared enqueue/dispatch logic used by all route handlers.

Route handlers call:
  JobController.run_or_enqueue(ctx, request, task_fn, output_ext, success_msg)

That single call decides sync vs async based on file size, runs or enqueues,
and returns a ready Flask Response.
"""

import logging
import time
import os
from typing import Callable, Optional

from flask import Request, Response

from config import Config
from core.context import JobContext
from core.exceptions import PDFWalaError
from core.pipeline import Pipeline
from core.result import Result
from core.metrics import metrics
from services.file_service import file_service
from services.queue_service import queue_service
from services.redis_service import redis_service
from services.auth_service import auth_service

log = logging.getLogger("pdfwala.controller")


class JobController:

    @staticmethod
    def run_or_enqueue(
        ctx:         JobContext,
        file_size:   int,
        task_fn:     Callable,
        output_ext:  str,
        success_msg: str,
        force_async: bool = False,
    ) -> Response:
        """
        Core dispatch logic:
          - If file_size > ASYNC_THRESHOLD or force_async=True → Celery
          - Otherwise → Pipeline.run() inline

        Always returns a Flask Response.
        """
        file_service.resolve_output_path(ctx, output_ext)

        # ── Persist initial state ────────────────────────────────────────
        redis_service.job_set(ctx.job_id, ctx.to_redis())

        if force_async or file_service.is_async(file_size):
            return JobController._enqueue(ctx, task_fn)
        else:
            return JobController._run_sync(ctx, success_msg)

    @staticmethod
    def _run_sync(ctx: JobContext, success_msg: str) -> Response:
        t0 = time.perf_counter()
        ok = True
        try:
            Pipeline.run(ctx)
            dur = (time.perf_counter() - t0) * 1000
            metrics.record(ctx.operation, dur, True)
            return Result.ok(ctx, success_msg)
        except PDFWalaError as ex:
            ok = False
            dur = (time.perf_counter() - t0) * 1000
            metrics.record(ctx.operation, dur, False)
            return Result.from_exception(ex, ctx.job_id)
        except Exception as ex:
            ok = False
            dur = (time.perf_counter() - t0) * 1000
            metrics.record(ctx.operation, dur, False)
            log.exception(f"[{ctx.job_id}] unhandled in sync run")
            return Result.error(str(ex), 500, ctx.job_id)

    @staticmethod
    def _enqueue(ctx: JobContext, task_fn: Callable) -> Response:
        try:
            queue_service.dispatch(ctx, task_fn)
            redis_service.job_set(ctx.job_id, ctx.to_redis())
            return Result.async_accepted(ctx)
        except Exception as ex:
            log.exception(f"[{ctx.job_id}] enqueue failed")
            return Result.error(f"Failed to queue job: {ex}", 500, ctx.job_id)

    @staticmethod
    def check_rate_limit(request: Request) -> Optional[Response]:
        """Returns a 429 Response if rate-limited, else None."""
        ip = (
            request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr
            or "unknown"
        )
        if redis_service.is_rate_limited(ip):
            remaining = redis_service.rate_limit_remaining(ip)
            return Result.error(
                "Rate limit exceeded. Try again in a minute.",
                429,
                extra={"remaining_requests": remaining},
            )
        return None

    @staticmethod
    def build_ctx(request: Request, operation: str) -> JobContext:
        """Create a fresh JobContext from the request."""
        ctx = JobContext()
        ctx.operation = operation
        ctx.user_id   = auth_service.get_user_id(request)
        ctx.params    = dict(request.form) | dict(request.args)
        # Flatten single-value lists from form data
        ctx.params = {
            k: v[0] if isinstance(v, list) and len(v) == 1 else v
            for k, v in ctx.params.items()
        }
        return ctx
