"""
workers/celery_app.py — PDFWala Enterprise V13.1 (PATCHED)

FIXES:
  - task_soft_time_limit and task_time_limit raised to match config.py values
  - worker_prefetch_multiplier kept at 1 (critical for fair load distribution)
  - Added broker_transport_options for Redis connection resilience
  - result_expires now matches REDIS_JOB_TTL
"""

from celery import Celery
from config import Config


def make_celery() -> Celery:
    app = Celery("pdfwala")
    app.conf.update(
        broker_url                         = Config.CELERY_BROKER_URL,
        result_backend                     = Config.CELERY_RESULT_BACKEND,
        broker_connection_retry_on_startup = True,
        # Redis transport options — important for resilience under load
        broker_transport_options           = {
            "visibility_timeout": Config.TASK_HARD_TIMEOUT + 60,
            "socket_timeout":     30,
            "socket_connect_timeout": 10,
        },
        task_serializer                    = "json",
        result_serializer                  = "json",
        accept_content                     = ["json"],
        task_soft_time_limit               = Config.TASK_SOFT_TIMEOUT,
        task_time_limit                    = Config.TASK_HARD_TIMEOUT,
        task_acks_late                     = True,
        task_reject_on_worker_lost         = True,
        task_default_queue                 = Config.QUEUE_FAST,
        task_queues                        = {
            Config.QUEUE_FAST:   {"exchange": Config.QUEUE_FAST},
            Config.QUEUE_OFFICE: {"exchange": Config.QUEUE_OFFICE},
            Config.QUEUE_SLOW:   {"exchange": Config.QUEUE_SLOW},
        },
        # CRITICAL: prefetch=1 ensures each worker takes one task at a time —
        # prevents a slow OCR job from blocking a fast compress job on same worker
        worker_prefetch_multiplier         = 1,
        worker_max_tasks_per_child         = 50,
        task_track_started                 = True,
        result_expires                     = Config.REDIS_JOB_TTL,
        task_max_retries                   = 2,
        task_default_retry_delay           = 10,
        # Disable unnecessary heartbeats to reduce Redis load
        worker_send_task_events            = False,
        task_send_sent_event               = False,
    )
    # Explicit import (instead of autodiscover) — autodiscover_tasks(["tasks"])
    # looks for `tasks.tasks` which doesn't exist. Importing the package runs
    # `tasks/__init__.py` which loads every submodule and registers all tasks.
    import tasks  # noqa: F401
    return app


celery_app = make_celery()
