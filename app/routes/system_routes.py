"""
app/routes/system_routes.py — PDFWala Enterprise V13.0
Download, health, job-status, and metrics endpoints.
"""

import os
import mimetypes
from datetime import datetime

from flask import Blueprint, request, send_file, jsonify

from config import Config
from core.context import JobContext
from core.result import Result
from core.metrics import metrics
from services.redis_service import redis_service

system_bp = Blueprint("system", __name__)


# ── Download ───────────────────────────────────────────────────────────────────

@system_bp.route("/download/<filename>")
def download(filename: str):
    # Sanitize: no path separators allowed
    if "/" in filename or "\\" in filename or ".." in filename:
        return Result.error("Invalid filename", 400)

    path = os.path.join(Config.OUTPUT_FOLDER, filename)
    if not os.path.exists(path):
        return Result.error("File not found or expired", 404)

    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return send_file(
        path,
        mimetype=mime,
        as_attachment=True,
        download_name=filename,
    )


# ── Health ─────────────────────────────────────────────────────────────────────

@system_bp.route("/health")
def health():
    checks = {}

    # Redis
    try:
        redis_service.ping()
        checks["redis"] = "ok"
    except Exception as ex:
        checks["redis"] = f"error: {ex}"

    # Disk
    try:
        stat = os.statvfs(Config.OUTPUT_FOLDER)
        free_gb = (stat.f_bavail * stat.f_frsize) / (1024 ** 3)
        checks["disk_free_gb"] = round(free_gb, 2)
        checks["disk"] = "ok" if free_gb > 1 else "low"
    except Exception as ex:
        checks["disk"] = f"error: {ex}"

    overall = "ok" if all(v == "ok" or isinstance(v, (int, float))
                          for v in checks.values()) else "degraded"
    return jsonify({
        "status":    overall,
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "checks":    checks,
    }), 200 if overall == "ok" else 503


# ── Job status ─────────────────────────────────────────────────────────────────

@system_bp.route("/jobs/<job_id>")
def job_status(job_id: str):
    if not job_id or len(job_id) > 64:
        return Result.error("Invalid job_id", 400)

    data = redis_service.job_get(job_id)
    if not data:
        return Result.error("Job not found or expired", 404)

    ctx      = JobContext.from_redis(data)
    payload  = {
        "success":   True,
        "job_id":    ctx.job_id,
        "operation": ctx.operation,
        "status":    ctx.status,
        "progress":  ctx.progress,
        "error":     ctx.error or None,
    }

    if ctx.status == "completed" and ctx.output_path:
        fname = os.path.basename(ctx.output_path)
        if os.path.exists(ctx.output_path):
            size = os.path.getsize(ctx.output_path)
            payload.update({
                "download_url": f"/download/{fname}",
                "filename":     fname,
                "size_bytes":   size,
            })
        payload.update(ctx.result)

    return jsonify(payload), 200


# ── Metrics ────────────────────────────────────────────────────────────────────

@system_bp.route("/metrics")
def get_metrics():
    op = request.args.get("operation")
    return jsonify(metrics.get_stats(op)), 200


# ── Ready / liveness (for k8s / docker healthcheck) ──────────────────────────

@system_bp.route("/ready")
def ready():
    return jsonify({"status": "ready"}), 200

@system_bp.route("/live")
def live():
    return jsonify({"status": "live"}), 200
