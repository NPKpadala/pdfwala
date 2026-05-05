"""
core/metrics.py — PDFWala Enterprise V13.0
Per-operation timing and success/failure counters. Redis-backed, in-memory fallback.
"""

import time
import threading
from typing import Dict, Any, Optional


class _InMemoryStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {}

    def hincrby(self, key, field, amount=1):
        with self._lock:
            self._data.setdefault(key, {})[field] = \
                self._data.get(key, {}).get(field, 0) + amount

    def hincrbyfloat(self, key, field, amount):
        with self._lock:
            self._data.setdefault(key, {})[field] = \
                self._data.get(key, {}).get(field, 0.0) + amount

    def hgetall(self, key):
        with self._lock:
            return {k: str(v) for k, v in self._data.get(key, {}).items()}

    def keys(self, pattern="*"):
        prefix = pattern.rstrip("*")
        with self._lock:
            return [k for k in self._data if k.startswith(prefix)]


class MetricsCollector:
    REDIS_KEY_PREFIX = "pdfwala:metrics:"
    GLOBAL_KEY       = "pdfwala:metrics:__global__"

    def __init__(self):
        self._store       = None
        self._fallback    = _InMemoryStore()
        self._initialized = False

    def _get_store(self):
        if self._initialized:
            return self._store
        try:
            from services.redis_service import redis_service
            redis_service.ping()
            self._store = redis_service
        except Exception:
            self._store = None
        self._initialized = True
        return self._store

    def _incr(self, key, field, amount=1):
        store = self._get_store()
        try:
            (store or self._fallback).hincrby(key, field, amount)
        except Exception:
            self._fallback.hincrby(key, field, amount)

    def _incrf(self, key, field, amount):
        store = self._get_store()
        try:
            (store or self._fallback).hincrbyfloat(key, field, amount)
        except Exception:
            self._fallback.hincrbyfloat(key, field, amount)

    def record(self, operation: str, duration_ms: float,
               success: bool, file_size_bytes: int = 0) -> None:
        key = f"{self.REDIS_KEY_PREFIX}{operation}"
        self._incr(key, "total")
        self._incr(key, "success" if success else "failure")
        self._incrf(key, "total_ms", duration_ms)
        if file_size_bytes:
            self._incrf(key, "total_bytes", float(file_size_bytes))
        self._incr(key, f"lat_{_latency_bucket(duration_ms)}")
        self._incr(self.GLOBAL_KEY, "total")
        self._incr(self.GLOBAL_KEY, "success" if success else "failure")
        self._incrf(self.GLOBAL_KEY, "total_ms", duration_ms)

    def get_stats(self, operation: str = None) -> Dict[str, Any]:
        store = self._get_store()

        def _read(key):
            try:
                return (store or self._fallback).hgetall(key) or {}
            except Exception:
                return self._fallback.hgetall(key)

        if operation:
            return _parse_op(_read(f"{self.REDIS_KEY_PREFIX}{operation}"), operation)

        try:
            all_keys = (store or self._fallback).keys(f"{self.REDIS_KEY_PREFIX}*")
        except Exception:
            all_keys = self._fallback.keys(f"{self.REDIS_KEY_PREFIX}*")

        ops = {}
        for k in all_keys:
            name = k.replace(self.REDIS_KEY_PREFIX, "")
            if name != "__global__":
                ops[name] = _parse_op(_read(k), name)

        return {"operations": ops, "global": _parse_op(_read(self.GLOBAL_KEY), "__global__")}

    def timer(self, operation: str):
        return _Timer(self, operation)


class _Timer:
    def __init__(self, collector, operation):
        self._c  = collector
        self._op = operation
        self._t0 = None
        self._ok = True

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def set_failed(self):
        self._ok = False

    def __exit__(self, exc_type, *_):
        if exc_type:
            self._ok = False
        self._c.record(self._op, (time.perf_counter() - self._t0) * 1000, self._ok)


def _latency_bucket(ms):
    if ms < 500:   return "lt500ms"
    if ms < 2000:  return "lt2s"
    if ms < 10000: return "lt10s"
    if ms < 30000: return "lt30s"
    return "gt30s"


def _parse_op(raw, name):
    total   = int(raw.get("total", 0))
    success = int(raw.get("success", 0))
    tot_ms  = float(raw.get("total_ms", 0.0))
    return {
        "operation":    name,
        "total":        total,
        "success":      success,
        "failure":      int(raw.get("failure", 0)),
        "success_rate": round(success / total * 100, 1) if total else 0.0,
        "avg_ms":       round(tot_ms / total, 1) if total else 0.0,
        "total_bytes":  int(float(raw.get("total_bytes", 0))),
        "latency_dist": {
            k.replace("lat_", ""): int(v)
            for k, v in raw.items() if k.startswith("lat_")
        },
    }


metrics = MetricsCollector()
