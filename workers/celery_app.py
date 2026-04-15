# === FILE: workers/celery_app.py ===
"""
PDFWala V10.0
workers/celery_app.py — Celery application factory.
"""

import os
from celery import Celery

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

celery_app = Celery(
    "pdfwala",
    broker=REDIS_URL,
    backend=REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    result_expires=3600,
    task_routes={
        "pdfwala.tasks.pdf_tasks.*":    {"queue": "fast"},
        "pdfwala.tasks.ocr_tasks.*":    {"queue": "slow"},
        "pdfwala.tasks.office_tasks.*": {"queue": "office"},
    },
)
