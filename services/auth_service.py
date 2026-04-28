import hmac
import logging
import ipaddress
from functools import wraps

from flask import request, jsonify, g

from config import Config
from services.redis_service import redis_service
from utils.security import verify_api_key

log = logging.getLogger("pdfwala.auth")

# Parse trusted proxies safely
def _parse_trusted_proxies():
    proxies = set()
    raw = getattr(Config, "TRUSTED_PROXY_IPS", "127.0.0.1").split(",")
    for item in raw:
        item = item.strip()
        if not item:
            continue
        try:
            # Supports both IP and CIDR
            proxies.add(ipaddress.ip_network(item, strict=False))
        except ValueError:
            log.warning(f"Invalid TRUSTED_PROXY_IPS entry ignored: {item}")
    return proxies

_TRUSTED_PROXY_NETS = _parse_trusted_proxies()


def _is_trusted_proxy(remote_addr: str) -> bool:
    """Check if request comes from trusted proxy."""
    if not remote_addr:
        return False
    try:
        ip = ipaddress.ip_address(remote_addr)
        return any(ip in net for net in _TRUSTED_PROXY_NETS)
    except ValueError:
        return False


def _get_client_ip() -> str:
    """Get real client IP safely."""
    remote = request.remote_addr or "unknown"

    if _is_trusted_proxy(remote):
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            # Take first valid IP only
            ip = xff.split(",")[0].strip()
            try:
                ipaddress.ip_address(ip)
                return ip
            except ValueError:
                log.warning(f"Invalid XFF IP ignored: {ip}")

    return remote


class AuthService:
    """Authentication helpers."""

    @staticmethod
    def verify_api_key(provided: str) -> bool:
        if not Config.API_KEY:
            return True
        return hmac.compare_digest(provided or "", Config.API_KEY)


def require_auth(f):
    """API Key authentication decorator."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not Config.API_KEY:
            g.user_id = "default"
            return f(*args, **kwargs)

        api_key = (
            request.headers.get("X-API-Key")
            or request.headers.get("Authorization", "").removeprefix("Bearer ")
        )

        if not api_key or not AuthService.verify_api_key(api_key):
            return jsonify({
                "success": False,
                "error": "Invalid or missing API key",
            }), 401

        g.user_id = "admin"
        return f(*args, **kwargs)

    return decorated


def require_rate_limit(f):
    """Rate limiter with Redis + safe fallback."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        user_id = getattr(g, "user_id", None)

        ip = _get_client_ip()
        key = user_id or ip

        limit = (
            Config.RATE_LIMIT_PRO
            if user_id and user_id != "anonymous"
            else Config.RATE_LIMIT_FREE
        )

        try:
            allowed = redis_service.rate_limit_check(key, limit)
        except Exception as ex:
            # FAIL-OPEN fallback (or change to fail-closed if needed)
            log.error(f"Redis rate limit failed: {ex}")
            allowed = True

        if not allowed:
            return jsonify({
                "success": False,
                "error": (
                    f"Rate limit exceeded ({limit} req/{Config.RATE_LIMIT_WINDOW_SEC}s). "
                    "Retry later."
                ),
            }), 429

        return f(*args, **kwargs)

    return wrapper
