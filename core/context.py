"""
core/context.py — PDFWala Enterprise V12.0
JobContext: the single object that flows through Route → Task → Pipeline → Engine.

Every layer reads from context and writes results back to it.
No layer passes raw file paths or loose parameters - everything travels in context.
"""

from __future__ import annotations
import uuid
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class JobContext:
    """
    Carries everything needed for one tool invocation across all layers.

    Created in the route handler (or controller).
    Serialised to Redis for async jobs.
    Deserialised in the Celery task.
    Passed to Pipeline.run() which calls the engine.
    """

    # ── Identity ────────────────────────────────────────────────────────────
    job_id:    str = field(default_factory=lambda: str(uuid.uuid4()))
    operation: str = ""          # e.g. "compress_pdf", "merge_pdf"
    user_id:   str = "anonymous"

    # ── File paths (set by pipeline, not caller) ─────────────────────────
    input_path:  str = ""        # absolute path to uploaded temp file
    input_paths: List[str] = field(default_factory=list)  # multi-file ops
    output_path: str = ""        # absolute path where engine writes output

    # ── Operation parameters (set by route handler) ──────────────────────
    params: Dict[str, Any] = field(default_factory=dict)

    # ── Runtime state (set by pipeline / engine) ─────────────────────────
    status:     str = "pending"  # pending | processing | completed | failed
    progress:   int = 0          # 0-100
    error:      str = ""
    result:     Dict[str, Any] = field(default_factory=dict)  # engine output

    # ── Timing ───────────────────────────────────────────────────────────
    created_at:   float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    # ── Internal flags ────────────────────────────────────────────────────
    is_async:  bool = False      # True when dispatched to Celery
    task_id:   str = ""          # Celery task ID

    def to_redis(self) -> Dict[str, str]:
        """Serialise to a flat Redis hash (all values as strings)."""
        import json
        return {
            "job_id":       self.job_id,
            "operation":    self.operation,
            "user_id":      self.user_id,
            "input_path":   self.input_path,
            "input_paths":  json.dumps(self.input_paths),
            "output_path":  self.output_path,
            "params":       json.dumps(self.params),
            "status":       self.status,
            "progress":     str(self.progress),
            "error":        self.error,
            "result":       json.dumps(self.result),
            "created_at":   str(self.created_at),
            "completed_at": str(self.completed_at or ""),
            "is_async":     str(self.is_async),
            "task_id":      self.task_id,
        }

    @classmethod
    def from_redis(cls, data: Dict[str, str]) -> "JobContext":
        """Deserialise from a flat Redis hash."""
        import json
        ctx = cls()
        ctx.job_id       = data.get("job_id", "")
        ctx.operation    = data.get("operation", "")
        ctx.user_id      = data.get("user_id", "anonymous")
        ctx.input_path   = data.get("input_path", "")
        ctx.input_paths  = json.loads(data.get("input_paths", "[]"))
        ctx.output_path  = data.get("output_path", "")
        ctx.params       = json.loads(data.get("params", "{}"))
        ctx.status       = data.get("status", "pending")
        ctx.progress     = int(data.get("progress", 0))
        ctx.error        = data.get("error", "")
        ctx.result       = json.loads(data.get("result", "{}"))
        ca = data.get("created_at", "")
        ctx.created_at   = float(ca) if ca else time.time()
        cmp = data.get("completed_at", "")
        ctx.completed_at = float(cmp) if cmp else None
        ctx.is_async     = data.get("is_async", "False") == "True"
        ctx.task_id      = data.get("task_id", "")
        return ctx

    def mark_processing(self):
        self.status = "processing"
        self.progress = 5

    def mark_completed(self, result: dict = None):
        self.status = "completed"
        self.progress = 100
        self.completed_at = time.time()
        if result:
            self.result.update(result)

    def mark_failed(self, error: str):
        self.status = "failed"
        self.error = error
        self.completed_at = time.time()

    def set_progress(self, pct: int, msg: str = ""):
        self.progress = max(0, min(100, pct))
