"""
PDFWala V10.0
services/auth_service.py — API key authentication and rate-limit decorators.
"""

import hmac
import logging
from functools import wraps

from flask import request, jsonify, g

from config import Config
from services.redis_service import redis_service
from utils.security import verify_api_key

log = logging.getLogger("pdfwala.auth")


class AuthService:
    """Authentication helpers."""

    @staticmethod
    def verify_api_key(provided: str) -> bool:
        if not Config.API_KEY:
            return True  # Open access in dev mode
        return verify_api_key(provided, Config.API_KEY)


def require_auth(f):
    """Decorator: API Key authentication via X-API-Key header."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not Config.API_KEY:
            g.user_id = "default"
            return f(*args, **kwargs)
        api_key = (
            request.headers.get("X-API-Key")
            or request.headers.get("Authorization", "").removeprefix("Bearer ")
        )
        if not api_key or not hmac.compare_digest(api_key, Config.API_KEY):
            return jsonify({
                "success": False,
                "error":   "Invalid or missing API key",
            }), 401
        g.user_id = "admin"
        return f(*args, **kwargs)
    return decorated


def require_rate_limit(f):
    """Decorator: Redis token-bucket rate limiter with in-memory fallback."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        user_id = getattr(g, "user_id", None)
        xff     = request.headers.get(
            "X-Forwarded-For", request.remote_addr or "unknown"
        )
        ip  = xff.split(",")[0].strip()
        key = user_id or ip

        limit = (
            Config.RATE_LIMIT_PRO
            if user_id and user_id != "anonymous"
            else Config.RATE_LIMIT_FREE
        )

        if not redis_service.rate_limit_check(key, limit):
            return jsonify({
                "success": False,
                "error":   f"Rate limit exceeded ({limit} req/{Config.RATE_LIMIT_WIN}s). "
                           f"Retry later.",
            }), 429

        return f(*args, **kwargs)
    return wrapper
