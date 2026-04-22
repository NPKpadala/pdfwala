"""
PDFWala Enterprise V11.0.0
workers/celery_app.py — Celery application factory.

Changes from V10:
  - Added include= so workers discover tasks without a separate autodiscover call
  - Added task_time_limit / task_soft_time_limit (30 min / 25 min)
  - Added broker_connection_retry_on_startup=True (Celery 6 compat)
  - REDIS_URL defaults to redis://redis:6379/0 (matches docker-compose)
"""

import os
from celery import Celery

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

celery_app = Celery(
    "pdfwala",
    broker=REDIS_URL,
    backend=REDIS_URL,
    # FIX: explicit include so workers load tasks without needing autodiscover
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
    # FIX: required in Celery 6+ to suppress deprecation warning and enable
    #      automatic reconnect on broker restart during worker startup
    broker_connection_retry_on_startup=True,
    # FIX V11: global hard / soft time limits (task-level decorators override these)
    task_time_limit=1800,        # 30 minutes hard kill
    task_soft_time_limit=1500,   # 25 minutes — raises SoftTimeLimitExceeded
    task_routes={
        # NOTE: routes must match the registered task *name*, not the module path
        "pdfwala.tasks.pdf_tasks.*":    {"queue": "fast"},
        "pdfwala.tasks.ocr_tasks.*":    {"queue": "slow"},
        "pdfwala.tasks.office_tasks.*": {"queue": "office"},
    },
)
