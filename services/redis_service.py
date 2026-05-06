"""
services/redis_service.py — PDFWala Enterprise V14.0

V14 FIXES:
  - Connection pool with max_connections=50 (was lazy single connection — exhausted under load)
  - socket_keepalive=True prevents stale connections after idle periods
  - health_check_interval added to auto-reconnect on network glitches
  - decode_responses=True pool used correctly
"""

import json
import time
import logging
from typing import Any, Dict, Optional

import redis
from redis import ConnectionPool
from config import Config

log = logging.getLogger("pdfwala.redis")


class RedisService:

    def __init__(self):
        self._pool: Optional[ConnectionPool] = None
        self._client: Optional[redis.Redis] = None

    def _get_pool(self) -> ConnectionPool:
        if self._pool is None:
            self._pool = ConnectionPool.from_url(
                Config.REDIS_URL,
                decode_responses=True,
                max_connections=Config.REDIS_MAX_CONNECTIONS,
                socket_connect_timeout=5,
                socket_timeout=10,
                socket_keepalive=True,
                socket_keepalive_options={},
                health_check_interval=30,
                retry_on_timeout=True,
            )
        return self._pool

    def _get(self) -> redis.Redis:
        # Each call gets a client backed by the shared pool — no new connections
        if self._client is None:
            self._client = redis.Redis(connection_pool=self._get_pool())
        return self._client

    def ping(self) -> bool:
        return self._get().ping()

    # ── Job CRUD ───────────────────────────────────────────────────────────

    def job_set(self, job_id: str, data: Dict[str, str]) -> None:
        key  = f"job:{job_id}"
        pipe = self._get().pipeline(transaction=False)
        pipe.hset(key, mapping=data)
        pipe.expire(key, Config.REDIS_JOB_TTL)
        pipe.execute()

    def job_get(self, job_id: str) -> Optional[Dict[str, str]]:
        data = self._get().hgetall(f"job:{job_id}")
        return data if data else None

    def job_update(self, job_id: str, fields: Dict[str, str]) -> None:
        key  = f"job:{job_id}"
        pipe = self._get().pipeline(transaction=False)
        pipe.hset(key, mapping=fields)
        pipe.expire(key, Config.REDIS_JOB_TTL)
        pipe.execute()

    def job_exists(self, job_id: str) -> bool:
        return self._get().exists(f"job:{job_id}") > 0

    def job_delete(self, job_id: str) -> None:
        self._get().delete(f"job:{job_id}")

    # ── Rate limiting ──────────────────────────────────────────────────────

    def is_rate_limited(self, identifier: str, rpm: int = None) -> bool:
        limit  = rpm or Config.RATE_LIMIT_RPM
        now    = time.time()
        window = 60
        key    = f"rl:{identifier}"
        pipe   = self._get().pipeline(transaction=True)
        try:
            pipe.zremrangebyscore(key, 0, now - window)
            pipe.zadd(key, {str(now): now})
            pipe.zcard(key)
            pipe.expire(key, window + 5)
            results = pipe.execute()
            return results[2] > limit
        except Exception as ex:
            log.warning(f"Rate-limit check failed for {identifier}: {ex}")
            return False

    def rate_limit_remaining(self, identifier: str, rpm: int = None) -> int:
        limit = rpm or Config.RATE_LIMIT_RPM
        now   = time.time()
        key   = f"rl:{identifier}"
        try:
            self._get().zremrangebyscore(key, 0, now - 60)
            return max(0, limit - self._get().zcard(key))
        except Exception:
            return limit

    # ── Generic ────────────────────────────────────────────────────────────

    def set(self, key: str, value: Any, ttl: int = None) -> None:
        v = json.dumps(value) if not isinstance(value, str) else value
        if ttl:
            self._get().setex(key, ttl, v)
        else:
            self._get().set(key, v)

    def get(self, key: str) -> Optional[str]:
        return self._get().get(key)

    def delete(self, key: str) -> None:
        self._get().delete(key)

    def keys(self, pattern: str) -> list:
        return self._get().keys(pattern)

    # ── Metrics helpers ────────────────────────────────────────────────────

    def hincrby(self, key: str, field: str, amount: int = 1) -> int:
        return self._get().hincrby(key, field, amount)

    def hincrbyfloat(self, key: str, field: str, amount: float) -> float:
        return self._get().hincrbyfloat(key, field, amount)

    def hgetall(self, key: str) -> Dict[str, str]:
        return self._get().hgetall(key) or {}


redis_service = RedisService()
