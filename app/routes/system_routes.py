"""
app/routes/system_routes.py — PDFWala Enterprise V13.0
Download, health, job-status, and metrics endpoints.
"""

import os
import mimetypes
from datetime import datetime
from pathlib import Path

from flask import Blueprint, request, send_file, jsonify, redirect

from config import Config
from core.context import JobContext
from core.result import Result
from core.metrics import metrics
from services.redis_service import redis_service

system_bp = Blueprint("system", __name__)


# ── Download ───────────────────────────────────────────────────────────────────

@system_bp.route("/download/<filename>")
def download(filename: str):
    # Resolve the full path and verify it stays within OUTPUT_FOLDER
    try:
        output_root = Path(Config.OUTPUT_FOLDER).resolve()
        path = (output_root / filename).resolve()
        if not str(path).startswith(str(output_root) + os.sep) and path != output_root:
            return Result.error("Invalid filename", 400)
    except (ValueError, OSError):
        return Result.error("Invalid filename", 400)

    if not path.exists():
        return Result.error("File not found or expired", 404)

    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return send_file(
        str(path),
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

# ── V12 backward compatibility ────────────────────────────────────────────
@system_bp.route("/api/v1/health")
def health_v12():
    return health()

@system_bp.route("/api/v1/ready")
def ready_v12():
    return ready()

@system_bp.route("/api/v1/live")
def live_v12():
    return live()

@system_bp.route("/api/v1/jobs/<job_id>")
def job_status_v12(job_id):
    return job_status(job_id)

# V12 backward compatibility — redirect old /api/* paths to /api/pdf/*
@system_bp.route("/api/compress", methods=["POST"])
def v12_compress(): return redirect("/api/pdf/compress", code=307)
@system_bp.route("/api/merge", methods=["POST"])
def v12_merge(): return redirect("/api/pdf/merge", code=307)
@system_bp.route("/api/split", methods=["POST"])
def v12_split(): return redirect("/api/pdf/split", code=307)
@system_bp.route("/api/rotate", methods=["POST"])
def v12_rotate(): return redirect("/api/pdf/rotate", code=307)
@system_bp.route("/api/watermark", methods=["POST"])
def v12_watermark(): return redirect("/api/pdf/watermark", code=307)
@system_bp.route("/api/protect", methods=["POST"])
def v12_protect(): return redirect("/api/pdf/protect", code=307)
@system_bp.route("/api/unlock", methods=["POST"])
def v12_unlock(): return redirect("/api/pdf/unlock", code=307)
@system_bp.route("/api/ocr", methods=["POST"])
def v12_ocr(): return redirect("/api/pdf/ocr", code=307)
@system_bp.route("/api/pdf-to-word", methods=["POST"])
def v12_pdf_to_word(): return redirect("/api/pdf/to-word", code=307)
@system_bp.route("/api/pdf-to-excel", methods=["POST"])
def v12_pdf_to_excel(): return redirect("/api/pdf/to-excel", code=307)
@system_bp.route("/api/image-to-pdf", methods=["POST"])
def v12_image_to_pdf(): return redirect("/api/image/to-pdf/multiple", code=307)
@system_bp.route("/api/pdf-to-image", methods=["POST"])
def v12_pdf_to_image(): return redirect("/api/pdf/to-image", code=307)
