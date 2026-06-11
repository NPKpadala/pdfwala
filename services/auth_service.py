"""
services/auth_service.py — PDFWala Enterprise V13.0
"""

import logging
from flask import Request
from config import Config

log = logging.getLogger("pdfwala.auth")


class AuthService:

    @staticmethod
    def get_user_id(request: Request) -> str:
        # Prefer proxy-set headers — CF-Connecting-IP (Cloudflare) and
        # X-Real-IP (nginx) are both set by our reverse proxy and overwrite
        # any value the client tried to inject. X-Forwarded-For is a fallback
        # because clients CAN spoof it, so it must come after the trusted ones.
        return (
            request.headers.get("X-User-ID")
            or request.headers.get("CF-Connecting-IP", "").strip()
            or request.headers.get("X-Real-IP", "").strip()
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr
            or "anonymous"
        )

    @staticmethod
    def check_api_key(request: Request) -> bool:
        if not Config.REQUIRE_API_KEY:
            return True
        key = request.headers.get(Config.API_KEY_HEADER, "")
        return key in Config.VALID_API_KEYS

    @staticmethod
    def require_api_key(request: Request) -> None:
        from core.exceptions import ValidationError
        if not AuthService.check_api_key(request):
            raise ValidationError("Invalid or missing API key")


auth_service = AuthService()
