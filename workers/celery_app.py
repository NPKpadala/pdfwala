"""
workers/celery_app.py — PDFWala Enterprise V13.0
"""

from celery import Celery
from config import Config


def make_celery() -> Celery:
    app = Celery("pdfwala")
    app.conf.update(
        broker_url                         = Config.CELERY_BROKER_URL,
        result_backend                     = Config.CELERY_RESULT_BACKEND,
        broker_connection_retry_on_startup = True,
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
        worker_prefetch_multiplier         = 1,
        worker_max_tasks_per_child         = 50,
        task_track_started                 = True,
        result_expires                     = Config.REDIS_JOB_TTL,
        task_max_retries                   = 2,
        task_default_retry_delay           = 10,
    )
    app.autodiscover_tasks(["tasks"])
    return app


celery_app = make_celery()
