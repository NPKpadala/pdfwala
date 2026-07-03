"""
services/visitors.py — lightweight unique-visitor tracking.

Uses Redis HyperLogLog (PFADD/PFCOUNT): O(1) per request, ~12 KB per day-key
regardless of traffic, and gives an accurate (~0.8% error) unique-IP count.

Exposed to the ops monitor via GET /metrics/visitors. Never raises into the
request path — a tracking failure must never break a user's PDF job.
"""

import logging
from datetime import datetime, timezone, timedelta

from services.redis_service import redis_service

log = logging.getLogger("pdfwala.visitors")

_PREFIX     = "pdfwala:visitors:"
_ALL_KEY    = _PREFIX + "all"          # all-time unique (no expiry)
_DAY_TTL    = 40 * 24 * 3600           # keep day-keys 40d so 7d/30d windows work


def _day_key(d: datetime) -> str:
    return _PREFIX + d.strftime("%Y%m%d")


def record_visit(ip: str) -> None:
    """Record one visitor IP into today's HLL + the all-time HLL. Best-effort."""
    if not ip:
        return
    try:
        r = redis_service._get()
        today = _day_key(datetime.now(timezone.utc))
        pipe = r.pipeline(transaction=False)
        pipe.pfadd(today, ip)
        pipe.expire(today, _DAY_TTL)
        pipe.pfadd(_ALL_KEY, ip)
        pipe.execute()
    except Exception as ex:          # never break the request path
        log.debug(f"record_visit skipped: {ex}")


def get_visitor_stats() -> dict:
    """Return unique-visitor counts: today, yesterday, last 7d, last 30d, total."""
    try:
        r = redis_service._get()
        now = datetime.now(timezone.utc)
        today_key = _day_key(now)
        yday_key  = _day_key(now - timedelta(days=1))
        keys_7  = [_day_key(now - timedelta(days=i)) for i in range(7)]
        keys_30 = [_day_key(now - timedelta(days=i)) for i in range(30)]
        pipe = r.pipeline(transaction=False)
        pipe.pfcount(today_key)
        pipe.pfcount(yday_key)
        pipe.pfcount(*keys_7)
        pipe.pfcount(*keys_30)
        pipe.pfcount(_ALL_KEY)
        today, yday, last7, last30, total = pipe.execute()
        return {
            "unique_visitors_today":     int(today or 0),
            "unique_visitors_yesterday": int(yday or 0),
            "unique_visitors_7d":        int(last7 or 0),
            "unique_visitors_30d":       int(last30 or 0),
            "unique_visitors_total":     int(total or 0),
            "as_of":                     now.isoformat(),
        }
    except Exception as ex:
        log.warning(f"get_visitor_stats failed: {ex}")
        return {
            "unique_visitors_today": 0, "unique_visitors_yesterday": 0,
            "unique_visitors_7d": 0, "unique_visitors_30d": 0,
            "unique_visitors_total": 0, "error": str(ex),
        }
