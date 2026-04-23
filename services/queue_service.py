"""
PDFWala V11.0.0
services/queue_service.py — Circuit breakers, backpressure, job management.
FIXED: Added cb_wkhtmltopdf circuit breaker.
"""

import time
import threading
import logging
from enum import Enum
from typing import Optional, Tuple

from config import Config

log = logging.getLogger("pdfwala.queue")


# ── Circuit Breaker ────────────────────────────────────────────────────────────

class CircuitBreaker:
    """Thread-safe half-open circuit breaker for external tool calls."""

    CLOSED    = "closed"
    OPEN      = "open"
    HALF_OPEN = "half_open"

    def __init__(self, name: str, failure_threshold: int = None,
                 recovery_timeout: int = None):
        self.name      = name
        self.threshold = failure_threshold or Config.CB_FAILURE_THRESHOLD
        self.recovery  = recovery_timeout  or Config.CB_RECOVERY_TIMEOUT
        self._state    = self.CLOSED
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == self.OPEN:
                if time.time() - self._opened_at > self.recovery:
                    self._state = self.HALF_OPEN
            return self._state

    def record_success(self):
        with self._lock:
            self._failures = 0
            self._state    = self.CLOSED

    def record_failure(self):
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                self._state     = self.OPEN
                self._opened_at = time.time()
                log.error(
                    f"CircuitBreaker[{self.name}] OPENED after "
                    f"{self._failures} failures"
                )

    def can_execute(self) -> bool:
        return self.state in (self.CLOSED, self.HALF_OPEN)

    def __repr__(self):
        return (
            f"<CircuitBreaker {self.name} "
            f"state={self.state} failures={self._failures}>"
        )


# Module-level circuit breaker instances
cb_libreoffice = CircuitBreaker("libreoffice")
cb_ghostscript = CircuitBreaker("ghostscript")
cb_tesseract   = CircuitBreaker("tesseract")
cb_wkhtmltopdf = CircuitBreaker("wkhtmltopdf")  # ADDED: V11.0.0


# ── Backpressure Controller ────────────────────────────────────────────────────

class BackpressureController:
    """
    Reject jobs when Celery queues are too deep.
    Prevents memory exhaustion under heavy load.
    """

    def __init__(self):
        self.max_depths = {
            "fast":   Config.CELERY_FAST_QUEUE_MAX,
            "office": Config.CELERY_OFFICE_QUEUE_MAX,
            "slow":   Config.CELERY_SLOW_QUEUE_MAX,
        }

    def can_accept(self, queue_name: str = "fast") -> Tuple[bool, str]:
        """
        Return (True, "OK") if the queue has capacity,
        or (False, reason) if overloaded.
        """
        from services.redis_service import redis_service
        rc = redis_service.client
        if rc is None:
            return True, "OK"  # Can't check depth → allow
        try:
            depth = rc.llen(queue_name)
            limit = self.max_depths.get(queue_name, 300)
            if depth > limit:
                return False, f"Queue '{queue_name}' full ({depth}/{limit})"
        except Exception:
            pass
        return True, "OK"


# ── Job Status / Priority Enums ────────────────────────────────────────────────

class JobPriority(Enum):
    HIGH   = 1
    NORMAL = 2
    LOW    = 3


class JobStatus(Enum):
    PENDING    = "pending"
    PROCESSING = "processing"
    COMPLETED  = "completed"
    FAILED     = "failed"


# ── QueueService ───────────────────────────────────────────────────────────────

class QueueService:
    """High-level job lifecycle management on top of RedisService."""

    def __init__(self):
        from services.redis_service import redis_service
        self._redis = redis_service

    def create_job(
        self,
        operation: str,
        user_id: str,
        priority: JobPriority = JobPriority.NORMAL,
    ) -> str:
        """Create a new job record. Returns job_id."""
        import uuid
        from utils.helpers import get_timestamp
        job_id = str(uuid.uuid4())
        self._redis.job_set(job_id, {
            "status":     JobStatus.PENDING.value,
            "progress":   "0",
            "operation":  operation,
            "created_at": get_timestamp(),
            "user_id":    user_id,
            "priority":   str(priority.value),
        })
        return job_id

    def update_progress(self, job_id: str, progress: int, message: str = ""):
        updates = {
            "status":   JobStatus.PROCESSING.value,
            "progress": str(progress),
        }
        if message:
            updates["message"] = message
        self._redis.job_update(job_id, updates)

    def complete_job(self, job_id: str, output_path: str):
        from utils.helpers import get_timestamp
        self._redis.job_update(job_id, {
            "status":       JobStatus.COMPLETED.value,
            "progress":     "100",
            "output_path":  output_path,
            "completed_at": get_timestamp(),
        })

    def fail_job(self, job_id: str, error: str):
        self._redis.job_update(job_id, {
            "status": JobStatus.FAILED.value,
            "error":  error,
        })

    def get_job(self, job_id: str) -> Optional[dict]:
        return self._redis.job_get(job_id)


# Module-level singletons
backpressure  = BackpressureController()
queue_service = QueueService()
