"""
core/result.py — PDFWala Enterprise V13.0
Unified JSON response builder for all routes.
"""

import os
from typing import Any, Dict

from flask import jsonify, Response
from config import Config


class Result:

    @staticmethod
    def ok(ctx, message: str, http_code: int = 200,
           extra: Dict[str, Any] = None) -> Response:
        payload: Dict[str, Any] = {
            "success": True,
            "message": message,
            "job_id":  ctx.job_id,
            "status":  ctx.status,
        }
        if ctx.output_path and os.path.exists(ctx.output_path):
            size  = os.path.getsize(ctx.output_path)
            fname = os.path.basename(ctx.output_path)
            payload.update({
                "download_url": f"/download/{fname}",
                "filename":     fname,
                "size_bytes":   size,
                "size_human":   _fmt_size(size),
                "expires_in":   f"{Config.FILE_TTL_SEC // 60} minutes",
            })
        if ctx.result:
            payload.update(ctx.result)
        if extra:
            payload.update(extra)
        return jsonify(payload), http_code

    @staticmethod
    def async_accepted(ctx, message: str = "Job queued") -> Response:
        return jsonify({
            "success":  True,
            "message":  message,
            "job_id":   ctx.job_id,
            "status":   "queued",
            "task_id":  ctx.task_id,
            "status_url": f"/jobs/{ctx.job_id}",
        }), 202

    @staticmethod
    def error(message: str, http_code: int = 500,
              job_id: str = None, extra: Dict[str, Any] = None) -> Response:
        payload: Dict[str, Any] = {"success": False, "error": message}
        if job_id:
            payload["job_id"] = job_id
        if extra:
            payload.update(extra)
        return jsonify(payload), http_code

    @staticmethod
    def from_exception(exc, job_id: str = None) -> Response:
        from core.exceptions import PDFWalaError
        if isinstance(exc, PDFWalaError):
            return Result.error(exc.message, exc.http_code, job_id)
        return Result.error(str(exc), 500, job_id)


def _fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
