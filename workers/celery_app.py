"""
PDFWala Enterprise V11.0.0
workers/celery_app.py — Celery application factory.
"""

import os
from celery import Celery

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

celery_app = Celery(
    "pdfwala",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=[
        "tasks.pdf_tasks",
        "tasks.ocr_tasks",
        "tasks.office_tasks",
    ],
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
    broker_connection_retry_on_startup=True,
    broker_connection_max_retries=10,          # V11.0.0: Don't retry forever
    task_reject_on_worker_lost=True,           # V11.0.0: Reject if worker dies
    task_time_limit=1800,        # 30 minutes hard kill
    task_soft_time_limit=1500,   # 25 minutes — raises SoftTimeLimitExceeded
    task_routes={
        "pdfwala.tasks.pdf_tasks.*":    {"queue": "fast"},
        "pdfwala.tasks.ocr_tasks.*":    {"queue": "slow"},
        "pdfwala.tasks.office_tasks.*": {"queue": "office"},
    },
)
