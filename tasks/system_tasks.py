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


@celery_app.task(name="tasks.system_tasks.cleanup_output_files")
def cleanup_output_files():
    """
    Delete output files older than Config.FILE_TTL_SEC.
    Also delete orphaned .tmp upload files older than 10 minutes.
    """
    if not Config.CLEANUP_ENABLED:
        return {"skipped": True}

    now     = time.time()
    deleted = 0
    errors  = 0

    # Clean output files
    output_dir = Config.OUTPUT_FOLDER
    if os.path.isdir(output_dir):
        for fname in os.listdir(output_dir):
            fpath = os.path.join(output_dir, fname)
            try:
                if os.path.isfile(fpath):
                    age = now - os.path.getmtime(fpath)
                    if age > Config.FILE_TTL_SEC:
                        os.remove(fpath)
                        deleted += 1
            except OSError as ex:
                log.warning(f"Cleanup: could not delete {fpath}: {ex}")
                errors += 1

    # Clean orphaned .tmp upload files (older than 10 minutes)
    upload_dir = Config.UPLOAD_FOLDER
    if os.path.isdir(upload_dir):
        for fname in os.listdir(upload_dir):
            if fname.endswith(".tmp"):
                fpath = os.path.join(upload_dir, fname)
                try:
                    age = now - os.path.getmtime(fpath)
                    if age > 600:   # 10 minutes
                        os.remove(fpath)
                        deleted += 1
                except OSError:
                    pass

    log.info(f"cleanup_output_files: deleted={deleted} errors={errors}")
    return {"deleted": deleted, "errors": errors}
