"""
core/pipeline.py — PDFWala Enterprise V12.0
THE single processing flow for every tool.

Every request, sync or async, passes through Pipeline.run().

Flow:
  1. validate()    — file type, size, PDF integrity
  2. load()        — save upload to temp, resolve output path
  3. execute()     — call the registered engine for this operation
  4. save_result() — persist result info to Redis
  5. cleanup()     — remove temp upload file

Engines register themselves via @Pipeline.register("operation_name").
The route only needs to call Pipeline.run(ctx) or Pipeline.enqueue(ctx).
"""

import os
import shutil
import logging
import time
import uuid
from typing import Callable, Dict

from config import Config
from core.context import JobContext
from core.exceptions import (
    PDFWalaError, ValidationError, ProcessingError
)
from services.redis_service import redis_service
from utils.helpers import generate_output_filename, format_file_size, get_timestamp

log = logging.getLogger("pdfwala.pipeline")

# ── Engine registry ────────────────────────────────────────────────────────────
_ENGINES: Dict[str, Callable[[JobContext], dict]] = {}


def register(operation: str):
    """
    Decorator to register an engine function for an operation.

    Usage in engine files:
        @register("compress_pdf")
        def compress_pdf(ctx: JobContext) -> dict:
            ...
    """
    def decorator(fn: Callable):
        _ENGINES[operation] = fn
        log.debug(f"Engine registered: {operation} → {fn.__qualname__}")
        return fn
    return decorator


def get_engine(operation: str) -> Callable:
    if operation not in _ENGINES:
        raise ProcessingError(
            f"No engine registered for operation '{operation}'. "
            f"Available: {sorted(_ENGINES.keys())}"
        )
    return _ENGINES[operation]


# ── Pipeline ───────────────────────────────────────────────────────────────────

class Pipeline:
    """
    Stateless pipeline. Call Pipeline.run(ctx) from tasks or sync routes.
    """

    @staticmethod
    def run(ctx: JobContext) -> JobContext:
        """
        Execute the full pipeline for the given context.
        Returns the same ctx object with status and result populated.
        Raises PDFWalaError on failure (caught by task or route handler).
        """
        log.info(f"[{ctx.job_id}] START {ctx.operation}")
        try:
            Pipeline._resolve_output_path(ctx)
            ctx.mark_processing()
            Pipeline._persist(ctx)

            engine_fn = get_engine(ctx.operation)
            result = engine_fn(ctx)

            ctx.mark_completed(result)
            Pipeline._persist(ctx)

            log.info(
                f"[{ctx.job_id}] DONE {ctx.operation} "
                f"→ {os.path.basename(ctx.output_path)}"
            )
            return ctx

        except PDFWalaError:
            raise  # let caller handle with proper HTTP code

        except Exception as ex:
            log.exception(f"[{ctx.job_id}] UNHANDLED in {ctx.operation}")
            raise ProcessingError(str(ex), cause=ex)

        finally:
            # Clean up input file(s) - output file stays for download
            Pipeline._cleanup_inputs(ctx)

    @staticmethod
    def _resolve_output_path(ctx: JobContext):
        """Build the output file path from input filename + operation."""
        if ctx.output_path:
            return  # already set (e.g. by async pre-queue logic)

        src = (
            os.path.basename(ctx.input_path)
            if ctx.input_path
            else (
                os.path.basename(ctx.input_paths[0])
                if ctx.input_paths
                else "file.bin"
            )
        )
        fname = generate_output_filename(
            src, ctx.operation,
            is_multi=bool(ctx.input_paths),
            filenames=[os.path.basename(p) for p in ctx.input_paths],
        )
        ctx.output_path = os.path.join(Config.OUTPUT_FOLDER, fname)

    @staticmethod
    def _persist(ctx: JobContext):
        """Write context state to Redis."""
        try:
            redis_service.job_set(ctx.job_id, ctx.to_redis())
        except Exception as ex:
            log.warning(f"[{ctx.job_id}] Redis persist failed: {ex}")

    @staticmethod
    def _cleanup_inputs(ctx: JobContext):
        """Remove uploaded temp input files after processing."""
        for path in ([ctx.input_path] + ctx.input_paths):
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError as ex:
                    log.warning(f"Cleanup failed for {path}: {ex}")

    # ── Async helpers ─────────────────────────────────────────────────────

    @staticmethod
    def save_upload_for_async(file_storage, ctx: JobContext) -> str:
        """
        Stream an uploaded file to disk for async processing.
        Returns the saved path and sets ctx.input_path.
        Uses streaming copy - never loads whole file into RAM.
        """
        ext = (
            file_storage.filename.rsplit(".", 1)[-1].lower()
            if "." in file_storage.filename
            else "bin"
        )
        path = os.path.join(Config.UPLOAD_FOLDER, f"{uuid.uuid4()}.{ext}")
        tmp = path + ".tmp"
        file_storage.seek(0)
        with open(tmp, "wb") as fh:
            shutil.copyfileobj(file_storage, fh, length=8192)
        os.rename(tmp, path)
        ctx.input_path = path
        return path

    @staticmethod
    def save_uploads_for_async(file_storages: list, ctx: JobContext) -> list:
        """Save multiple uploaded files for async processing."""
        paths = []
        for fs in file_storages:
            ext = (
                fs.filename.rsplit(".", 1)[-1].lower()
                if "." in fs.filename
                else "bin"
            )
            path = os.path.join(Config.UPLOAD_FOLDER, f"{uuid.uuid4()}.{ext}")
            tmp = path + ".tmp"
            fs.seek(0)
            with open(tmp, "wb") as fh:
                shutil.copyfileobj(fs, fh, length=8192)
            os.rename(tmp, path)
            paths.append(path)
        ctx.input_paths = paths
        return paths

    @staticmethod
    def build_ok_response(ctx: JobContext, message: str) -> dict:
        """Build the standard success response dict from a completed context."""
        payload = {
            "success":  True,
            "message":  message,
            "job_id":   ctx.job_id,
            "status":   ctx.status,
        }
        if ctx.output_path and os.path.exists(ctx.output_path):
            from utils.security import generate_signed_url
            size = os.path.getsize(ctx.output_path)
            payload.update({
                "download_url": f"/download/{os.path.basename(ctx.output_path)}",
                "signed_url":   generate_signed_url(ctx.output_path),
                "filename":     os.path.basename(ctx.output_path),
                "size_human":   format_file_size(size),
                "expires_in":   f"{Config.FILE_TTL_SEC // 60} minutes",
            })
        payload.update(ctx.result)
        return payload
