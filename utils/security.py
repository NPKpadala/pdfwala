"""
PDFWala V10.0
utils/security.py — HMAC signed URLs, ReDoS-safe regex, API key helpers.
"""

import re
import time
import hmac
import secrets
import hashlib
import threading
from typing import Optional

from config import Config

# ── Redaction preset patterns ─────────────────────────────────────────────────
REDACTION_PATTERNS = {
    "ssn":         r'\b\d{3}-\d{2}-\d{4}\b',
    "email":       r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b',
    "phone":       r'\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b',
    "aadhaar":     r'\b\d{4}\s?\d{4}\s?\d{4}\b',
    "pan":         r'\b[A-Z]{5}\d{4}[A-Z]\b',
    "credit_card": r'\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b',
}


def generate_signed_url(filepath: str, expiry: int = None) -> str:
    """Generate HMAC-SHA256-signed time-limited download URL."""
    filename  = filepath.split("/")[-1]  # os.path.basename
    expiry_ts = int(time.time()) + (expiry or Config.SIGNED_URL_EXPIRY)
    msg       = f"{filename}:{expiry_ts}".encode()
    signature = hmac.new(
        Config.SIGNED_URL_SECRET.encode(), msg, hashlib.sha256
    ).hexdigest()[:24]
    return f"/download/{filename}?expires={expiry_ts}&sig={signature}"


def verify_signed_url(filename: str, expires: str, signature: str) -> bool:
    """Verify a signed URL. Returns False if expired or tampered."""
    try:
        expiry_ts = int(expires)
        if time.time() > expiry_ts:
            return False
        msg      = f"{filename}:{expiry_ts}".encode()
        expected = hmac.new(
            Config.SIGNED_URL_SECRET.encode(), msg, hashlib.sha256
        ).hexdigest()[:24]
        return hmac.compare_digest(signature, expected)
    except Exception:
        return False


def generate_api_key() -> str:
    """Generate a cryptographically random API key (32 bytes hex)."""
    return secrets.token_hex(32)


def verify_api_key(provided: str, expected: str) -> bool:
    """Constant-time comparison of API keys."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)


class SafeRegex:
    """
    ReDoS-safe regex wrapper.
    Rejects patterns with nested quantifiers and wraps match/search with timeout.
    Formerly _safe_regex_compile().
    """

    _DANGEROUS = re.compile(r'(\w+[+*]\w*[+*]|\(.*[+*].*\)[+*])')

    def __init__(self, pattern: str, timeout_seconds: float = 1.0):
        if self._DANGEROUS.search(pattern):
            raise ValueError(
                "Pattern rejected: potential ReDoS vulnerability detected"
            )
        self._pat     = re.compile(pattern)
        self._timeout = timeout_seconds

    def _run_with_timeout(self, method, *args):
        result = [None]
        error  = [None]

        def _runner():
            try:
                result[0] = method(*args)
            except Exception as ex:
                error[0] = ex

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join(self._timeout)
        if t.is_alive():
            raise TimeoutError(f"Regex timed out after {self._timeout}s")
        if error[0]:
            raise error[0]
        return result[0]

    def search(self, s: str):
        return self._run_with_timeout(self._pat.search, s)

    def match(self, s: str):
        return self._run_with_timeout(self._pat.match, s)

    def finditer(self, s: str):
        """finditer is lazy; collect lazily with a per-call approach."""
        return self._pat.finditer(s)

    @classmethod
    def compile(cls, pattern: str, timeout_seconds: float = 1.0) -> "SafeRegex":
        """Factory — equivalent to _safe_regex_compile()."""
        return cls(pattern, timeout_seconds)
