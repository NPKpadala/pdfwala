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
        ctx.input_path = dest
        return size

    @staticmethod
    def save_multiple(request: Request, ctx: JobContext,
                      field: str = "files") -> int:
        files = request.files.getlist(field)
        if not files:
            raise ValidationError(f"No files uploaded (field='{field}')")
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
            paths.append(dest)
            total += size
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
