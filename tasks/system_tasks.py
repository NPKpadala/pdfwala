"""
tasks/system_tasks.py — PDFWala Enterprise V14.0 (NEW)

V14: Automatic output file cleanup task.
Runs via Celery Beat every hour. Deletes output files older than FILE_TTL_SEC.
Without this, the output/ directory fills up and the server runs out of disk.
"""

import logging
import os
import time

from workers.celery_app import celery_app
from config import Config

log = logging.getLogger("pdfwala.tasks.system")


def _sweep_dir(directory: str, max_age_seconds: int, only_suffix: str = ""):
    """Delete files under `directory` older than the age threshold.

    Optionally restrict to files with a given suffix (e.g. '.tmp').
    Returns (deleted, errors).
    """
    deleted = errors = 0
    if not os.path.isdir(directory):
        return deleted, errors
    now = time.time()
    for fname in os.listdir(directory):
        if only_suffix and not fname.endswith(only_suffix):
            continue
        fpath = os.path.join(directory, fname)
        try:
            if not os.path.isfile(fpath):
                continue
            if now - os.path.getmtime(fpath) > max_age_seconds:
                os.remove(fpath)
                deleted += 1
        except OSError as ex:
            log.warning(f"cleanup: could not delete {fpath}: {ex}")
            errors += 1
    return deleted, errors


@celery_app.task(name="tasks.system_tasks.cleanup_output_files")
def cleanup_output_files():
    """
    Hourly cleanup task. Three sweeps:

      1. OUTPUT_FOLDER : completed outputs older than FILE_TTL_SEC (2 h).
      2. UPLOAD_FOLDER : completed uploads older than 1 hour. Sync mode
                         means the input file is no longer needed once the
                         response goes out — without this sweep these
                         accumulate forever (this is what filled the
                         uploads volume to 24 MB of stale 1-day-old files).
      3. UPLOAD_FOLDER : orphaned `.tmp` fragments older than 10 minutes
                         (interrupted multipart uploads).
      4. TEMP_FOLDER   : anything older than 1 hour (engine scratch space).
    """
    if not Config.CLEANUP_ENABLED:
        return {"skipped": True}

    deleted = errors = 0
    upload_ttl_sec = int(os.getenv("UPLOAD_TTL_SEC", "3600"))
    temp_ttl_sec   = int(os.getenv("TEMP_TTL_SEC",   "3600"))

    for sweep in (
        (Config.OUTPUT_FOLDER, Config.FILE_TTL_SEC, ""),     # outputs (TTL_SEC)
        (Config.UPLOAD_FOLDER, upload_ttl_sec,      ""),     # completed uploads
        (Config.UPLOAD_FOLDER, 600,                 ".tmp"), # orphan fragments
        (Config.TEMP_FOLDER,   temp_ttl_sec,        ""),     # engine temp scratch
    ):
        d, e = _sweep_dir(*sweep)
        deleted += d; errors += e

    log.info(f"cleanup_output_files: deleted={deleted} errors={errors}")
    return {"deleted": deleted, "errors": errors}
