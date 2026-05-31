"""
services/file_service.py — PDFWala Enterprise V13.0
Upload handling: save FileStorage to disk, decide sync vs async.
"""

import os
import shutil
import uuid
import logging
from pathlib import Path

from flask import Request
from config import Config
from core.context import JobContext
from core.exceptions import ValidationError

log = logging.getLogger("pdfwala.file_service")


class FileService:

    @staticmethod
    def save_single(request: Request, ctx: JobContext,
                    field: str = "file") -> int:
        f = request.files.get(field)
        if not f or not f.filename:
            raise ValidationError(f"No file uploaded (field='{field}')")
        ext  = Path(f.filename).suffix.lower() or ".bin"
        dest = os.path.join(Config.UPLOAD_FOLDER, f"{uuid.uuid4()}{ext}")
        tmp  = dest + ".tmp"
        f.seek(0)
        with open(tmp, "wb") as fh:
            shutil.copyfileobj(f, fh, length=65536)
        os.rename(tmp, dest)
        size = os.path.getsize(dest)
        if size == 0:
            os.remove(dest)
            raise ValidationError("Uploaded file is empty")
        # 10 MB free-tier cap — defence in depth (nginx + Flask
        # MAX_CONTENT_LENGTH already enforce this, but we re-check here so
        # the user gets the same friendly message regardless of which layer
        # the request hits first).
        if size > Config.MAX_FILE_SIZE:
            os.remove(dest)
            max_mb = Config.MAX_FILE_SIZE // (1024 * 1024)
            size_mb = size / (1024 * 1024)
            raise ValidationError(
                f"File too large ({size_mb:.1f} MB). Maximum size is "
                f"{max_mb}MB for free use — try Compress PDF first, or "
                f"use Split PDF to break it into smaller pieces."
            )
        ctx.input_path = dest
        return size

    @staticmethod
    def save_multiple(request: Request, ctx: JobContext,
                      field: str = "files") -> int:
        files = request.files.getlist(field)
        if not files:
            raise ValidationError(f"No files uploaded (field='{field}')")
        max_per   = Config.MAX_FILE_SIZE
        max_total = max_per * 2   # 20 MB total across multi-file ops
        max_mb    = max_per // (1024 * 1024)
        paths = []
        total = 0
        for f in files:
            if not f or not f.filename:
                continue
            ext  = Path(f.filename).suffix.lower() or ".bin"
            dest = os.path.join(Config.UPLOAD_FOLDER, f"{uuid.uuid4()}{ext}")
            tmp  = dest + ".tmp"
            f.seek(0)
            with open(tmp, "wb") as fh:
                shutil.copyfileobj(f, fh, length=65536)
            os.rename(tmp, dest)
            size = os.path.getsize(dest)
            if size == 0:
                os.remove(dest)
                continue
            if size > max_per:
                os.remove(dest)
                for p in paths:        # clean partial state
                    try: os.remove(p)
                    except OSError: pass
                raise ValidationError(
                    f"One of the files is too large ({size / (1024*1024):.1f} MB). "
                    f"Maximum size per file is {max_mb}MB for free use."
                )
            paths.append(dest)
            total += size
            if total > max_total:
                for p in paths:
                    try: os.remove(p)
                    except OSError: pass
                raise ValidationError(
                    f"Combined upload is too large "
                    f"({total / (1024*1024):.1f} MB). Maximum total is "
                    f"{max_total // (1024*1024)}MB across all files."
                )
        if not paths:
            raise ValidationError("No valid files uploaded")
        ctx.input_paths = paths
        return total

    @staticmethod
    def is_async(size_bytes: int) -> bool:
        return size_bytes > Config.ASYNC_THRESHOLD

    @staticmethod
    def resolve_output_path(ctx: JobContext, ext: str) -> str:
        fname = f"{ctx.operation}_{ctx.job_id[:8]}.{ext.lstrip('.')}"
        ctx.output_path = os.path.join(Config.OUTPUT_FOLDER, fname)
        return ctx.output_path


file_service = FileService()
