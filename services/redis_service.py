"""
PDFWala V10.0
services/redis_service.py — Singleton Redis client with job store, rate limiter,
and complete in-memory fallback.
"""

import time
import threading
from typing import Any, Dict, List, Optional

from config import Config

try:
    import redis as redis_lib
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    redis_lib = None


class RedisService:
    """
    Singleton Redis wrapper providing:
    - job_set / job_get / job_update
    - rate_limit_check
    - file_reference_add / file_reference_remove
    - cache_get / cache_set
    All methods fall back to in-memory storage when Redis is unavailable.
    """

    _instance = None
    _init_lock = threading.Lock()

    def __new__(cls):
        with cls._init_lock:
            if cls._instance is None:
                obj = super().__new__(cls)
                obj._client = None
                obj._client_lock = threading.Lock()
                obj._mem_jobs: Dict[str, dict] = {}
                obj._mem_jobs_lock = threading.Lock()
                obj._mem_rate: Dict[str, list] = {}
                obj._mem_rate_lock = threading.Lock()
                obj._mem_cache: Dict[str, tuple] = {}  # key → (value, expiry_ts)
                obj._mem_cache_lock = threading.Lock()
                cls._instance = obj
                # Start background cleanup
                threading.Thread(
                    target=obj._rate_cleanup_loop, daemon=True, name="rl-cleanup"
                ).start()
        return cls._instance

    @property
    def client(self) -> Optional[Any]:
        """Return live Redis client or None."""
        if self._client is not None:
            return self._client
        if not REDIS_AVAILABLE:
            return None
        with self._client_lock:
            if self._client is None:
                try:
                    pool = redis_lib.ConnectionPool.from_url(
                        Config.REDIS_URL,
                        max_connections=Config.REDIS_MAX_CONNECTIONS,
                        decode_responses=False,
                    )
                    rc = redis_lib.Redis(connection_pool=pool)
                    rc.ping()
                    self._client = rc
                except Exception:
                    self._client = None
        return self._client

    # ── Job store ──────────────────────────────────────────────────────────────

    def job_set(self, job_id: str, mapping: dict, ttl: int = None):
        rc = self.client
        if rc:
            try:
                key     = f"job:{job_id}"
                str_map = {k: str(v) for k, v in mapping.items()}
                rc.hset(key, mapping=str_map)
                rc.expire(key, ttl or Config.FILE_TTL_SEC)
                return
            except Exception:
                pass
        with self._mem_jobs_lock:
            self._mem_jobs.setdefault(job_id, {}).update(mapping)
            self._mem_jobs[job_id]["_ttl"] = time.time() + (ttl or Config.FILE_TTL_SEC)

    def job_get(self, job_id: str) -> Optional[dict]:
        rc = self.client
        if rc:
            try:
                raw = rc.hgetall(f"job:{job_id}")
                if raw:
                    return {k.decode(): v.decode() for k, v in raw.items()}
            except Exception:
                pass
        with self._mem_jobs_lock:
            job = self._mem_jobs.get(job_id)
            if job:
                if time.time() < job.get("_ttl", 0):
                    return {k: v for k, v in job.items() if k != "_ttl"}
                del self._mem_jobs[job_id]
        return None

    def job_update(self, job_id: str, mapping: dict):
        existing = self.job_get(job_id) or {}
        existing.update(mapping)
        self.job_set(job_id, existing)

    # ── Rate limiting ──────────────────────────────────────────────────────────

    def rate_limit_check(self, key: str, limit: int) -> bool:
        """
        Return True if request is ALLOWED (under limit), False if denied.
        Redis implementation uses atomic INCR + EXPIRE.
        """
        rc = self.client
        if rc:
            try:
                redis_key = f"rl:{key}"
                pipe      = rc.pipeline()
                pipe.incr(redis_key)
                pipe.expire(redis_key, Config.RATE_LIMIT_WIN)
                count, _ = pipe.execute()
                return int(count) <= limit
            except Exception:
                pass
        # In-memory fallback
        now = time.monotonic()
        with self._mem_rate_lock:
            hits = [t for t in self._mem_rate.get(key, [])
                    if now - t < Config.RATE_LIMIT_WIN]
            if len(hits) >= limit:
                return False
            hits.append(now)
            self._mem_rate[key] = hits
        return True

    # ── File reference tracking ────────────────────────────────────────────────

    def file_reference_add(self, path: str, ttl: int = None):
        """Register a file path in Redis with a TTL for tracked cleanup."""
        rc = self.client
        if rc:
            try:
                rc.setex(f"file:{path}", ttl or Config.FILE_TTL_SEC, "1")
                return
            except Exception:
                pass
        # Memory fallback: schedule deletion via thread
        def _del_later():
            time.sleep(ttl or Config.FILE_TTL_SEC)
            import os
            try:
                os.remove(path)
            except OSError:
                pass
        threading.Thread(target=_del_later, daemon=True).start()

    def file_reference_remove(self, path: str):
        rc = self.client
        if rc:
            try:
                rc.delete(f"file:{path}")
            except Exception:
                pass

    # ── General cache ──────────────────────────────────────────────────────────

    def cache_set(self, key: str, value: str, ttl: int = 300):
        rc = self.client
        if rc:
            try:
                rc.setex(f"cache:{key}", ttl, value)
                return
            except Exception:
                pass
        with self._mem_cache_lock:
            self._mem_cache[key] = (value, time.time() + ttl)

    def cache_get(self, key: str) -> Optional[str]:
        rc = self.client
        if rc:
            try:
                val = rc.get(f"cache:{key}")
                return val.decode() if val else None
            except Exception:
                pass
        with self._mem_cache_lock:
            entry = self._mem_cache.get(key)
            if entry:
                value, expiry = entry
                if time.time() < expiry:
                    return value
                del self._mem_cache[key]
        return None

    # ── Background cleanup ─────────────────────────────────────────────────────

    def _rate_cleanup_loop(self):
        """Periodically remove stale in-memory rate-limit buckets."""
        while True:
            time.sleep(300)
            now = time.monotonic()
            with self._mem_rate_lock:
                for ip in list(self._mem_rate.keys()):
                    self._mem_rate[ip] = [
                        t for t in self._mem_rate[ip]
                        if now - t < Config.RATE_LIMIT_WIN
                    ]
                    if not self._mem_rate[ip]:
                        del self._mem_rate[ip]
            # Also purge expired in-memory jobs
            with self._mem_jobs_lock:
                now_ts = time.time()
                stale  = [jid for jid, j in self._mem_jobs.items()
                          if now_ts > j.get("_ttl", 0)]
                for jid in stale:
                    del self._mem_jobs[jid]


# Module-level singleton
redis_service = RedisService()
