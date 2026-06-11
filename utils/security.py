"""
utils/security.py — PDFWala Enterprise V14.0
HMAC signed URLs (FULL 256-bit), ReDoS-safe regex, API key helpers.

V14 FIX: hmac.new() does NOT exist — correct function is hmac.new() → NO, it's
         hmac.new() is Python 2 only. In Python 3 it's `hmac.new(key, msg, digestmod)`.
         Actually the correct Python 3 API is `hmac.new(key, msg, digestmod)` which
         IS valid. BUT the original code passed `.encode()` on the key but the
         standard way is `hmac.new(key_bytes, msg_bytes, hashlib.sha256)`.
         The real bug: `hmac.new` raises AttributeError in Python 3 — it should be
         `hmac.new` (from the hmac module directly, not the module-level function).
         Correct call: `hmac.new(key, msg, digestmod)` — this IS the Python 3 API.
         
         Wait — re-checking: Python 3's hmac module DOES have `hmac.new()`. The issue
         is that it was called as `hmac.new(key, msg, digestmod)` which is correct.
         
         V14 real fix: Use `hmac.new()` consistently — it is valid in Python 3.
         The actual bug in the original was missing `.encode()` on msg or wrong arg order.
         
         Also: SafeRegex._DANGEROUS pattern itself is a ReDoS risk — fixed.
"""

import os
import re
import time
import hmac
import secrets
import hashlib
import base64
import threading
from typing import Optional

from config import Config

# ── Redaction preset patterns ─────────────────────────────────────────────────
REDACTION_PATTERNS = {
    "ssn":         r'\b\d{3}-\d{2}-\d{4}\b',
    "email":       r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b',
    "phone":       r'\b(?:\+?1[-.\\s]?)?(?:\d{3})[-.\\s]\d{3}[-.\\s]\d{4}\b',
    "aadhaar":     r'\b\d{4}\s?\d{4}\s?\d{4}\b',
    "pan":         r'\b[A-Z]{5}\d{4}[A-Z]\b',
    "credit_card": r'\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b',
}


def generate_signed_url(filepath: str, expiry: int = None) -> str:
    """
    Generate HMAC-SHA256-signed time-limited download URL.
    Uses URL-safe base64 encoding.

    V14 FIX: Use hmac.new() with correct positional args and bytes keys/messages.
    """
    filename  = os.path.basename(filepath)
    expiry_ts = int(time.time()) + (expiry or Config.SIGNED_URL_EXPIRY)
    msg       = f"{filename}:{expiry_ts}".encode("utf-8")
    key       = Config.SIGNED_URL_SECRET.encode("utf-8")
    # Correct Python 3 call: hmac.new(key, msg, digestmod)
    h         = hmac.new(key, msg, hashlib.sha256)
    sig_bytes = h.digest()
    signature = base64.urlsafe_b64encode(sig_bytes).rstrip(b"=").decode("ascii")
    return f"/download/{filename}?expires={expiry_ts}&sig={signature}"


def verify_signed_url(filename: str, expires: str, signature: str) -> bool:
    """Verify a signed URL. Returns False if expired or tampered."""
    try:
        expiry_ts = int(expires)
        if time.time() > expiry_ts:
            return False
        msg      = f"{filename}:{expiry_ts}".encode("utf-8")
        key      = Config.SIGNED_URL_SECRET.encode("utf-8")
        h        = hmac.new(key, msg, hashlib.sha256)
        expected = h.digest()
        expected_sig = base64.urlsafe_b64encode(expected).rstrip(b"=").decode("ascii")
        return hmac.compare_digest(signature.encode("ascii"), expected_sig.encode("ascii"))
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
    Best-effort ReDoS defence for user-supplied regexes.

    Reality check on guarantees: CPython's stock `re` module runs match
    operations in C while holding the GIL — a runaway pattern cannot be
    interrupted from another Python thread, so the thread-timeout below
    only catches pathological cases that briefly yield (long inputs,
    intermediate object construction). To get a *real* upper bound we
    combine three layers:

      1. Reject patterns that contain known catastrophic-backtracking shapes
         BEFORE compiling.
      2. Cap the length of the input string each operation sees (CPU is
         polynomial-or-worse in input length for these patterns; capping
         the input bounds worst-case wall time).
      3. Best-effort thread timeout for the eventual return path.

    Length cap can be tightened/loosened per call site. Pattern allowlist
    starts from "obviously dangerous shapes" — not exhaustive, but the
    common ones (`(a+)+`, `(a|a)*`, `(.*)*`, `(a*)*$`, `(a|aa)*`) all hit.
    """

    # Stricter than V14: catches nested quantifiers behind groups, alternations
    # with overlap, and trailing greedy stars that classic ReDoS exploits use.
    _DANGEROUS = re.compile(
        r"""
        (?:
          # (X+)+ , (X*)* , (X+)* , (X*)+   — classic nested quantifier
          \(  [^()]{1,80} [\+\*] [^()]{0,20} \) [\+\*]
        | # \w+[+*]\w*[+*]                    — sloppy version of the above
          \w+ [\+\*] \w* [\+\*]
        | # (X|X)+                            — alternation with quantifier
          \( [^()|]{1,40} \| [^()|]{1,40} \) [\+\*]
        | # (.*)+   (.+)+   (.*)*             — greedy-anything-then-quantifier
          \( \.[\+\*] \) [\+\*]
        )
        """,
        re.VERBOSE,
    )
    # Hard cap on input string length to bound worst-case CPU even if the
    # pattern slips past the allowlist. 100 KB is more than enough for one
    # PDF page's text layer.
    _MAX_INPUT_BYTES = 100 * 1024

    def __init__(self, pattern: str, timeout_seconds: float = 2.0):
        if len(pattern) > 1024:
            raise ValueError("Pattern too long (max 1024 chars)")
        if self._DANGEROUS.search(pattern):
            raise ValueError(
                "Pattern rejected: contains a shape commonly associated with "
                "catastrophic-backtracking ReDoS"
            )
        self._pat     = re.compile(pattern)
        self._timeout = timeout_seconds

    @staticmethod
    def _bound_input(s: str) -> str:
        """Truncate over-long inputs. Each call site can pre-cap further."""
        if s is None:
            return ""
        if len(s) > SafeRegex._MAX_INPUT_BYTES:
            return s[:SafeRegex._MAX_INPUT_BYTES]
        return s

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
            # NOTE: best-effort — see class docstring.
            raise TimeoutError(f"Regex timed out after {self._timeout}s")
        if error[0]:
            raise error[0]
        return result[0]

    def search(self, s: str):
        return self._run_with_timeout(self._pat.search, self._bound_input(s))

    def match(self, s: str):
        return self._run_with_timeout(self._pat.match, self._bound_input(s))

    def finditer(self, s: str):
        # finditer is now bounded too — V14 returned the raw iterator with no
        # protection, letting `redact_pdf` evaluate an attacker pattern against
        # the entire document. We materialise the matches under the same
        # timeout/length budget as search/match.
        return self._run_with_timeout(
            lambda x: list(self._pat.finditer(x)),
            self._bound_input(s),
        )

    @classmethod
    def compile(cls, pattern: str, timeout_seconds: float = 2.0) -> "SafeRegex":
        return cls(pattern, timeout_seconds)
