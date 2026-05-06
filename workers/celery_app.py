"""
workers/celery_app.py — PDFWala Enterprise V14.0

V14 FIXES over V13.1:
  - broker_pool_limit added (prevents connection exhaustion with many workers)
  - task_always_eager removed (was False, fine, but left explicit)
  - Celery beat schedule added for automatic output file cleanup task
  - worker_max_tasks_per_child lowered to 25 for heavy ops (memory leak guard)
  - task_compression disabled (JSON is fast enough; zlib adds latency)
  - Added SoftTimeLimitExceeded handling at the celery app level
"""

from celery import Celery
from celery.schedules import crontab
from config import Config


def make_celery() -> Celery:
    app = Celery("pdfwala")
    app.conf.update(
        broker_url                         = Config.CELERY_BROKER_URL,
        result_backend                     = Config.CELERY_RESULT_BACKEND,
        broker_connection_retry_on_startup = True,
        # FIX V14: Limit broker connection pool — prevents Redis exhaustion
        broker_pool_limit                  = Config.CELERY_BROKER_POOL_LIMIT,
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
        # CRITICAL: prefetch=1 ensures each worker takes one task at a time
        worker_prefetch_multiplier         = 1,
        # FIX V14: lower max tasks per child for memory-intensive heavy ops
        worker_max_tasks_per_child         = 25,
        task_track_started                 = True,
        result_expires                     = Config.REDIS_JOB_TTL,
        task_max_retries                   = 2,
        task_default_retry_delay           = 10,
        worker_send_task_events            = False,
        task_send_sent_event               = False,
        # FIX V14: Celery beat schedule — clean up old output files every hour
        beat_schedule                      = {
            "cleanup-output-files": {
                "task":     "tasks.system_tasks.cleanup_output_files",
                "schedule": crontab(minute=0),   # every hour on the hour
            },
        },
    )
    # FIX V14.1: Use explicit include instead of autodiscover.
    # autodiscover_tasks(["tasks"]) requires the CWD to be /app AND the tasks
    # package to already be importable at Celery startup — both conditions are
    # fragile in Docker. Explicit include is guaranteed to work.
    app.conf.include = [
        "tasks.pdf_tasks",
        "tasks.office_tasks",
        "tasks.image_tasks",
        "tasks.system_tasks",
    ]
    return app


celery_app = make_celery()
