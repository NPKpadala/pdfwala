#!/usr/bin/env python3
"""
PDFWala Production Monitor v5
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Patches applied:
  1. Security: removed hardcoded credentials, env-var only
  2. Error handling: specific exceptions, custom hierarchy, retry/backoff
  3. Testing infrastructure: see test_monitor.py
  4. Rate limiting: priority queue, burst, batching
  5. Config management: dedicated Config class
  6. Memory/perf: LogHashStore TTL cleanup, GC, self-monitoring, jitter
  + CPU opts: non-blocking cpu_percent, docker stats batch, container cache
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations

import collections
import gc
import gzip
import hashlib
import json
import logging
import os
import queue
import random
import re
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import zipfile
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, Tuple

# ── optional deps ─────────────────────────────────────────────
try:
    import psutil
except ImportError:
    print("❌ psutil required: pip install psutil", file=sys.stderr)
    sys.exit(1)

try:
    import requests
except ImportError:
    print("❌ requests required: pip install requests", file=sys.stderr)
    sys.exit(1)


# ╔══════════════════════════════════════════════════════════════
# ║  CUSTOM EXCEPTION HIERARCHY  (Prompt 2)
# ╚══════════════════════════════════════════════════════════════

class MonitorError(Exception):
    """Base exception for all monitor errors."""
    code: str = "MONITOR_ERR"

class AlertError(MonitorError):
    code = "ALERT_ERR"

class CommandError(MonitorError):
    code = "CMD_ERR"

class NetworkError(MonitorError):
    code = "NET_ERR"

class TimeoutError_(MonitorError):
    code = "TIMEOUT_ERR"

class PermissionError_(MonitorError):
    code = "PERM_ERR"

class DockerError(MonitorError):
    code = "DOCKER_ERR"


# ╔══════════════════════════════════════════════════════════════
# ║  CONFIG CLASS  (Prompt 5)
# ║  Priority: env vars > config file > hardcoded defaults
# ║
# ║  Set credentials via environment variables:
# ║    export TELEGRAM_TOKEN="your_bot_token"
# ║    export TELEGRAM_CHAT_ID="your_chat_id"
# ║  Or place them in a systemd EnvironmentFile, Docker env,
# ║  or your shell's ~/.bashrc / ~/.profile.
# ╚══════════════════════════════════════════════════════════════

class Config:
    """Centralised, validated, documented configuration."""

    _DEFAULTS: Dict[str, object] = {
        # --- Telegram (NO defaults — must be set via env) ---
        "TELEGRAM_TOKEN":   None,
        "TELEGRAM_CHAT_ID": None,

        # --- Thresholds ---
        "CPU_THRESHOLD":    90,
        "RAM_THRESHOLD":    90,
        "DISK_THRESHOLD":   90,
        "SWAP_THRESHOLD":   50,
        "FD_THRESHOLD":     80,
        "LOAD_MULTIPLIER":  1.5,
        "UPLOAD_SIZE_GB":   10.0,
        "OUTPUT_SIZE_GB":   15.0,
        "TEMP_SIZE_GB":     5.0,
        "DISK_IO_BUSY_PCT": 90,
        "SSL_WARN_DAYS":    30,
        "SSL_CRIT_DAYS":    7,
        "MEM_TREND_CYCLES": 5,
        "MEM_TREND_DELTA":  3.0,
        "CELERY_QUEUE_WARN": 200,
        "CELERY_QUEUE_CRIT": 500,

        # --- Timing ---
        "CHECK_INTERVAL":        60,
        "SUMMARY_INTERVAL":      21600,
        "ALERT_COOLDOWN":        1800,
        "AUTO_RESTART_COOLDOWN": 300,
        "CMD_POLL_INTERVAL":     3,

        # --- Log bundle ---
        "LOG_BUNDLE_HOUR":   2,
        "LOG_BUNDLE_MINUTE": 0,
        "LOG_ARCHIVE_DAYS":  7,

        # --- Telegram rate limit ---
        "TG_RATE_MAX":    20,
        "TG_RATE_WINDOW": 60,
        "TG_BURST_MAX":   3,      # burst allowance before enforcing rate limit

        # --- Paths ---
        "BASE_DIR": "/home/opc/pdfwala",

        # --- Performance / memory tuning (Prompt 6 / CPU opts) ---
        "LOG_TAIL_LINES":            50,
        "CACHE_TTL":                 30,    # container status cache seconds
        "MEMORY_WARN_THRESHOLD_MB":  100,
        "ENABLE_PERF_METRICS":       False,
        "NGINX_TAIL_LINES":          1000,  # limit nginx log parsing
        "LOG_HASH_TTL":              86400, # seconds before a log hash expires
    }

    # Type map for validation
    _INT_KEYS = {
        "CPU_THRESHOLD","RAM_THRESHOLD","DISK_THRESHOLD","SWAP_THRESHOLD",
        "FD_THRESHOLD","DISK_IO_BUSY_PCT","SSL_WARN_DAYS","SSL_CRIT_DAYS",
        "MEM_TREND_CYCLES","CELERY_QUEUE_WARN","CELERY_QUEUE_CRIT",
        "CHECK_INTERVAL","SUMMARY_INTERVAL","ALERT_COOLDOWN",
        "AUTO_RESTART_COOLDOWN","CMD_POLL_INTERVAL","LOG_BUNDLE_HOUR",
        "LOG_BUNDLE_MINUTE","LOG_ARCHIVE_DAYS","TG_RATE_MAX","TG_RATE_WINDOW",
        "TG_BURST_MAX","LOG_TAIL_LINES","CACHE_TTL","MEMORY_WARN_THRESHOLD_MB",
        "NGINX_TAIL_LINES","LOG_HASH_TTL",
    }
    _FLOAT_KEYS = {
        "LOAD_MULTIPLIER","UPLOAD_SIZE_GB","OUTPUT_SIZE_GB",
        "TEMP_SIZE_GB","MEM_TREND_DELTA",
    }
    _BOOL_KEYS = {"ENABLE_PERF_METRICS"}

    def __init__(self, config_file: Optional[Path] = None):
        self._lock = threading.Lock()
        self._data: Dict[str, object] = dict(self._DEFAULTS)
        self._change_log: List[str] = []

        # Load config file (lowest non-default priority)
        if config_file and config_file.exists():
            self._load_file(config_file)

        # Override with environment variables (highest priority)
        self._load_env()

        # Validate
        self._validate()

    def _load_file(self, path: Path):
        try:
            raw = json.loads(path.read_text())
            for k, v in raw.items():
                if k in self._data:
                    self._data[k] = v
        except Exception as e:
            print(f"⚠️  Config file load failed ({path}): {e}", file=sys.stderr)

    def _load_env(self):
        # Credential env vars use different names for clarity
        token = os.getenv("TELEGRAM_TOKEN") or os.getenv("TOKEN")
        chat  = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID")
        if token:
            self._data["TELEGRAM_TOKEN"] = token.strip()
        if chat:
            self._data["TELEGRAM_CHAT_ID"] = chat.strip()

        for k in self._DEFAULTS:
            if k in ("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID"):
                continue
            raw = os.getenv(k)
            if raw is None:
                continue
            try:
                if k in self._INT_KEYS:
                    self._data[k] = int(raw)
                elif k in self._FLOAT_KEYS:
                    self._data[k] = float(raw)
                elif k in self._BOOL_KEYS:
                    self._data[k] = raw.lower() in ("1", "true", "yes")
                else:
                    self._data[k] = raw
            except ValueError:
                print(f"⚠️  Invalid env var {k}={raw!r}, using default",
                      file=sys.stderr)

    def _validate(self):
        errs = []
        if not self._data.get("TELEGRAM_TOKEN"):
            errs.append(
                "TELEGRAM_TOKEN is not set.\n"
                "  → export TELEGRAM_TOKEN='your_bot_token'\n"
                "  → Or add it to your systemd EnvironmentFile / Docker env"
            )
        if not self._data.get("TELEGRAM_CHAT_ID"):
            errs.append(
                "TELEGRAM_CHAT_ID is not set.\n"
                "  → export TELEGRAM_CHAT_ID='your_chat_id'"
            )
        if self._data["CHECK_INTERVAL"] < 10:
            errs.append(f"CHECK_INTERVAL={self._data['CHECK_INTERVAL']} too low (min 10)")
        if self._data["ALERT_COOLDOWN"] < 60:
            errs.append(f"ALERT_COOLDOWN={self._data['ALERT_COOLDOWN']} too low (min 60)")
        if os.geteuid() != 0:
            errs.append(
                "Monitor must run as root (docker socket not accessible).\n"
                "  → sudo python3 monitor.py\n"
                "  → Or use a systemd service running as root"
            )
        if errs:
            for e in errs:
                print(f"❌ Config: {e}", file=sys.stderr)
            sys.exit(1)

    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value, source: str = "runtime"):
        with self._lock:
            old = self._data.get(key)
            self._data[key] = value
            entry = (f"[{datetime.now(timezone.utc).isoformat()}] "
                     f"{source}: {key} {old!r} → {value!r}")
            self._change_log.append(entry)
            logging.getLogger("pdfwala.monitor").info(
                "Config change: %s", entry)

    def change_log(self) -> List[str]:
        with self._lock:
            return list(self._change_log)

    def as_dict(self) -> dict:
        with self._lock:
            return {k: ("***" if "TOKEN" in k else v)
                    for k, v in self._data.items()}

    # Convenience properties for frequently accessed values
    @property
    def TOKEN(self) -> str:
        return self._data["TELEGRAM_TOKEN"]

    @property
    def CHAT_ID(self) -> str:
        return self._data["TELEGRAM_CHAT_ID"]


# ── Global config instance ────────────────────────────────────
# Loaded once at startup; all module code reads from CFG.*
CFG = Config(config_file=Path("/home/opc/pdfwala/monitor_config.json"))

# ── Convenience aliases (keep existing code readable) ─────────
def _ei(k, d): return CFG.get(k, d)
def _ef(k, d): return CFG.get(k, d)

TOKEN   = CFG.TOKEN
CHAT_ID = CFG.CHAT_ID

CPU_THRESHOLD    = CFG.get("CPU_THRESHOLD")
RAM_THRESHOLD    = CFG.get("RAM_THRESHOLD")
DISK_THRESHOLD   = CFG.get("DISK_THRESHOLD")
SWAP_THRESHOLD   = CFG.get("SWAP_THRESHOLD")
FD_THRESHOLD     = CFG.get("FD_THRESHOLD")
LOAD_MULTIPLIER  = CFG.get("LOAD_MULTIPLIER")
UPLOAD_SIZE_GB   = CFG.get("UPLOAD_SIZE_GB")
OUTPUT_SIZE_GB   = CFG.get("OUTPUT_SIZE_GB")
TEMP_SIZE_GB     = CFG.get("TEMP_SIZE_GB")
DISK_IO_PCT      = CFG.get("DISK_IO_BUSY_PCT")
SSL_WARN_DAYS    = CFG.get("SSL_WARN_DAYS")
SSL_CRIT_DAYS    = CFG.get("SSL_CRIT_DAYS")
MEM_TREND_CYCLES = CFG.get("MEM_TREND_CYCLES")
MEM_TREND_DELTA  = CFG.get("MEM_TREND_DELTA")
CELERY_QUEUE_WARN = CFG.get("CELERY_QUEUE_WARN")
CELERY_QUEUE_CRIT = CFG.get("CELERY_QUEUE_CRIT")
CHECK_INTERVAL        = CFG.get("CHECK_INTERVAL")
SUMMARY_INTERVAL      = CFG.get("SUMMARY_INTERVAL")
ALERT_COOLDOWN        = CFG.get("ALERT_COOLDOWN")
AUTO_RESTART_COOLDOWN = CFG.get("AUTO_RESTART_COOLDOWN")
CMD_POLL_INTERVAL     = CFG.get("CMD_POLL_INTERVAL")
LOG_BUNDLE_HOUR   = CFG.get("LOG_BUNDLE_HOUR")
LOG_BUNDLE_MINUTE = CFG.get("LOG_BUNDLE_MINUTE")
LOG_ARCHIVE_DAYS  = CFG.get("LOG_ARCHIVE_DAYS")
TG_RATE_MAX    = CFG.get("TG_RATE_MAX")
TG_RATE_WINDOW = CFG.get("TG_RATE_WINDOW")
LOG_TAIL_LINES           = CFG.get("LOG_TAIL_LINES")
CACHE_TTL                = CFG.get("CACHE_TTL")
MEMORY_WARN_THRESHOLD_MB = CFG.get("MEMORY_WARN_THRESHOLD_MB")
ENABLE_PERF_METRICS      = CFG.get("ENABLE_PERF_METRICS")
NGINX_TAIL_LINES         = CFG.get("NGINX_TAIL_LINES")

BASE_DIR        = Path(CFG.get("BASE_DIR"))
LOG_FILE        = BASE_DIR / "monitor.log"
UPLOADS_DIR     = BASE_DIR / "uploads"
OUTPUTS_DIR     = BASE_DIR / "outputs"
TEMP_DIR        = BASE_DIR / "temp"
METRICS_FILE    = BASE_DIR / "monitor_metrics.json"
HEARTBEAT_FILE  = BASE_DIR / "monitor_heartbeat"
LOG_HASH_FILE   = BASE_DIR / "monitor_log_hashes.json"
LOG_ARCHIVE_DIR = BASE_DIR / "log_archives"

EXPECTED_CONTAINERS = [
    "pdfwala-app",
    "pdfwala-worker-fast",
    "pdfwala-worker-office",
    "pdfwala-worker-slow",
    "pdfwala-nginx",
    "pdfwala-redis",
]

CONTAINER_ALIASES: Dict[str, str] = {
    "app":    "pdfwala-app",
    "worker": "pdfwala-worker-fast",
    "fast":   "pdfwala-worker-fast",
    "office": "pdfwala-worker-office",
    "slow":   "pdfwala-worker-slow",
    "nginx":  "pdfwala-nginx",
    "redis":  "pdfwala-redis",
}

REDIS_CONTAINER = "pdfwala-redis"
CELERY_QUEUES   = ["fast", "office", "slow"]

ENDPOINTS: List[Tuple[str, str, int, bool]] = [
    ("Homepage",   "https://npkpadala.com/pdfwala/", 200, True),
    ("API Health", "https://npkpadala.com/health",   200, True),
]

HOSTNAME = socket.gethostname()


# ╔══════════════════════════════════════════════════════════════
# ║  LOGGING
# ╚══════════════════════════════════════════════════════════════

BASE_DIR.mkdir(parents=True, exist_ok=True)
LOG_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("pdfwala.monitor")


# ╔══════════════════════════════════════════════════════════════
# ║  SEVERITY + ALERT
# ╚══════════════════════════════════════════════════════════════

class Sev:
    WARN = "WARNING"
    CRIT = "CRITICAL"

_EMOJI = {Sev.WARN: "⚠️", Sev.CRIT: "🚨"}

# Priority levels for rate limiter (Prompt 4)
class Priority:
    DEBUG     = 0
    INFO      = 1
    WARNING   = 2
    CRITICAL  = 3
    EMERGENCY = 4


class Alert:
    def __init__(self, key: str, message: str,
                 severity: str = Sev.WARN,
                 auto_action: Optional[str] = None):
        self.key         = key
        self.message     = message
        self.severity    = severity
        self.auto_action = auto_action
        self.ts          = datetime.now(timezone.utc)

    def __str__(self):
        return f"{_EMOJI[self.severity]} {self.message}"


# ╔══════════════════════════════════════════════════════════════
# ║  TELEGRAM  (Prompt 4: priority queue, burst, batching)
# ╚══════════════════════════════════════════════════════════════

class _PriMsg:
    """Internal priority-queue item."""
    __slots__ = ("priority", "text", "parse_mode", "chat_id", "ts")
    def __init__(self, priority, text, parse_mode, chat_id):
        self.priority   = priority
        self.text       = text
        self.parse_mode = parse_mode
        self.chat_id    = chat_id
        self.ts         = time.monotonic()

    def __lt__(self, other):
        # Higher priority first; ties broken by earlier timestamp
        if self.priority != other.priority:
            return self.priority > other.priority
        return self.ts < other.ts


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self._token   = token
        self._chat_id = chat_id
        self._lock    = threading.Lock()

        # Sliding window of send timestamps (Prompt 6: bounded deque)
        self._sends: Deque[float] = deque(maxlen=TG_RATE_MAX + 10)

        self._url     = f"https://api.telegram.org/bot{token}/sendMessage"
        self._doc_url = f"https://api.telegram.org/bot{token}/sendDocument"
        self._upd_url = f"https://api.telegram.org/bot{token}/getUpdates"

        # Prompt 4: priority message queue
        import heapq
        self._msg_queue: List[_PriMsg] = []
        self._queue_lock  = threading.Lock()
        self._rate_limit_hits = 0
        self._batched_warnings: List[str] = []

        # Prompt 6: burst tracker
        self._burst_ts: Deque[float] = deque(maxlen=CFG.get("TG_BURST_MAX", 3))

        # Background sender thread
        self._sender = threading.Thread(
            target=self._sender_loop, daemon=True, name="tg-sender"
        )
        self._sender.start()

    # ── rate-limit helpers ────────────────────────────────────

    def _under_limit(self) -> bool:
        now = time.monotonic()
        # Clean window
        while self._sends and now - self._sends[0] > TG_RATE_WINDOW:
            self._sends.popleft()
        return len(self._sends) < TG_RATE_MAX

    def _burst_ok(self) -> bool:
        """Allow up to TG_BURST_MAX messages before enforcing full rate limit."""
        now = time.monotonic()
        burst_max = CFG.get("TG_BURST_MAX", 3)
        # Purge burst entries older than 5 seconds
        while self._burst_ts and now - self._burst_ts[0] > 5.0:
            self._burst_ts.popleft()
        return len(self._burst_ts) < burst_max

    # ── public send (Prompt 4: priority-aware) ────────────────

    def send(self, text: str, parse_mode: str = "HTML",
             chat_id: Optional[str] = None,
             priority: int = Priority.INFO) -> bool:
        """
        Queue a message for sending. CRITICAL/EMERGENCY bypass rate limits.
        Returns True if queued (not necessarily delivered).
        """
        target = chat_id or self._chat_id
        if len(text) > 4000:
            text = text[:3900] + "\n…[truncated]"
        msg = _PriMsg(priority, text, parse_mode, target)
        with self._queue_lock:
            import heapq
            heapq.heappush(self._msg_queue, msg)
        return True

    def _sender_loop(self):
        """Background thread: drain priority queue respecting rate limits."""
        import heapq
        while True:
            try:
                with self._queue_lock:
                    if not self._msg_queue:
                        pass
                    else:
                        msg = heapq.heappop(self._msg_queue)
                        self._deliver(msg)
            except Exception as e:
                log.debug("Sender loop error: %s", e)
            time.sleep(0.1)

    def _deliver(self, msg: _PriMsg):
        """Actually send one message, with rate-limit and burst logic."""
        is_critical = msg.priority >= Priority.CRITICAL
        with self._lock:
            rate_ok  = self._under_limit()
            burst_ok = self._burst_ok()

        if not is_critical and not rate_ok and not burst_ok:
            # Rate limited non-critical: batch it
            self._rate_limit_hits += 1
            log.warning("[NET_ERR] Telegram rate limited — batching warning")
            with self._lock:
                self._batched_warnings.append(msg.text[:200])
            # Flush batch if it's grown large enough
            if len(self._batched_warnings) >= 5:
                self._flush_batch(msg.chat_id)
            return

        # If there are batched warnings waiting, flush them first
        if self._batched_warnings and is_critical:
            self._flush_batch(msg.chat_id)

        with self._lock:
            self._sends.append(time.monotonic())
            self._burst_ts.append(time.monotonic())

        self._send_with_retry(msg.text, msg.parse_mode, msg.chat_id)

    def _flush_batch(self, chat_id: str):
        if not self._batched_warnings:
            return
        batch = self._batched_warnings[:]
        self._batched_warnings.clear()
        combined = (
            f"⚠️ <b>Batched warnings ({len(batch)}) — rate limited</b>\n\n"
            + "\n---\n".join(batch[:10])
        )
        self._send_with_retry(combined, "HTML", chat_id)

    def _send_with_retry(self, text: str, parse_mode: str,
                         chat_id: str, max_attempts: int = 3) -> bool:
        """Send with exponential backoff (Prompt 2)."""
        for attempt in range(max_attempts):
            try:
                r = requests.post(
                    self._url,
                    json={"chat_id": chat_id, "text": text,
                          "parse_mode": parse_mode},
                    timeout=15,
                )
                if r.status_code == 200:
                    return True
                if r.status_code == 429:
                    retry_after = r.json().get("parameters", {}).get(
                        "retry_after", 2 ** attempt)
                    log.warning("[NET_ERR] Telegram 429 — retry after %ss",
                                retry_after)
                    time.sleep(retry_after)
                    continue
                log.error("[NET_ERR] Telegram HTTP %s: %s",
                          r.status_code, r.text[:100])
            except requests.exceptions.Timeout:
                log.error("[TIMEOUT_ERR] Telegram attempt %d timed out",
                          attempt + 1)
                time.sleep(2 ** attempt)
            except requests.exceptions.ConnectionError as e:
                log.error("[NET_ERR] Telegram connection error attempt %d: %s",
                          attempt + 1, e)
                time.sleep(2 ** attempt)
            except requests.exceptions.RequestException as e:
                log.error("[NET_ERR] Telegram request error attempt %d: %s",
                          attempt + 1, e)
                time.sleep(2 ** attempt)
        return False

    def send_document(self, file_path: Path, caption: str = "") -> bool:
        if not file_path.exists():
            log.error("[CMD_ERR] send_document: file not found: %s", file_path)
            return False
        with self._lock:
            if not self._under_limit():
                log.warning("[NET_ERR] Telegram rate limit — document send suppressed")
                return False
            self._sends.append(time.monotonic())
        for attempt in range(3):
            try:
                with file_path.open("rb") as fh:
                    r = requests.post(
                        self._doc_url,
                        data={"chat_id": self._chat_id,
                              "caption": caption[:1024]},
                        files={"document": (file_path.name, fh,
                                            "application/zip")},
                        timeout=120,
                    )
                if r.status_code == 200:
                    log.info("Log bundle sent to Telegram: %s", file_path.name)
                    return True
                log.error("[NET_ERR] Telegram doc %s: %s",
                          r.status_code, r.text[:200])
            except requests.exceptions.Timeout:
                log.error("[TIMEOUT_ERR] Telegram doc attempt %d timed out",
                          attempt + 1)
                time.sleep(2 ** attempt)
            except requests.exceptions.RequestException as e:
                log.error("[NET_ERR] Telegram doc attempt %d: %s", attempt + 1, e)
                time.sleep(2 ** attempt)
        return False

    def get_updates(self, offset: int, timeout: int = 2) -> List[dict]:
        try:
            r = requests.get(
                self._upd_url,
                params={"offset": offset, "timeout": timeout,
                        "allowed_updates": ["message"]},
                timeout=timeout + 5,
            )
            if r.status_code == 200:
                return r.json().get("result", [])
        except requests.exceptions.Timeout:
            log.debug("[TIMEOUT_ERR] getUpdates timed out")
        except requests.exceptions.RequestException as e:
            log.debug("[NET_ERR] getUpdates error: %s", e)
        return []

    def test(self) -> bool:
        return self._send_with_retry(
            "🔍 PDFWala Monitor v5 — credential test OK", "HTML", self._chat_id
        )

    @property
    def rate_limit_hits(self) -> int:
        return self._rate_limit_hits


# ╔══════════════════════════════════════════════════════════════
# ║  COOLDOWN TRACKER
# ╚══════════════════════════════════════════════════════════════

class Cooldown:
    def __init__(self, seconds: int):
        self._cd   = seconds
        self._map: Dict[str, Dict] = {}
        self._lock = threading.Lock()

    def should_fire(self, key: str) -> bool:
        now = datetime.now(timezone.utc)
        with self._lock:
            st = self._map.get(key)
            if st is None:
                self._map[key] = {"last": now, "count": 1}
                return True
            if (now - st["last"]).total_seconds() >= self._cd:
                st["last"]   = now
                st["count"] += 1
                return True
            return False

    def clear(self, key: str):
        with self._lock:
            self._map.pop(key, None)

    def active_count(self) -> int:
        with self._lock:
            return len(self._map)


# ╔══════════════════════════════════════════════════════════════
# ║  LOG HASH STORE  (Prompt 6: TTL-based cleanup, LRU eviction)
# ╚══════════════════════════════════════════════════════════════

class LogHashStore:
    MAX = 2000
    TTL = CFG.get("LOG_HASH_TTL", 86400)   # default 24 h

    def __init__(self, path: Path):
        self._path   = path
        # {hash: expiry_unix_timestamp}
        self._hashes: Dict[str, float] = {}
        self._lock   = threading.Lock()
        self._load()

    def _load(self):
        try:
            if self._path.exists():
                d = json.loads(self._path.read_text())
                now  = time.time()
                raw  = d.get("hashes", [])
                # Support both old set-format and new [[hash,expiry]] format
                if raw and isinstance(raw[0], list):
                    self._hashes = {h: exp for h, exp in raw if exp > now}
                else:
                    self._hashes = {h: now + self.TTL for h in raw}
                log.info("Loaded %d log hashes", len(self._hashes))
        except (json.JSONDecodeError, OSError, ValueError) as e:
            log.warning("[MONITOR_ERR] Log hash load failed: %s", e)

    def _save(self):
        try:
            now = time.time()
            # Evict expired
            self._hashes = {h: exp for h, exp in self._hashes.items()
                            if exp > now}
            # LRU eviction if still over MAX
            if len(self._hashes) > self.MAX:
                items = sorted(self._hashes.items(), key=lambda x: x[1],
                               reverse=True)
                self._hashes = dict(items[:self.MAX])
            self._path.write_text(
                json.dumps({"hashes": [[h, e] for h, e in self._hashes.items()]})
            )
        except OSError as e:
            log.warning("[MONITOR_ERR] Log hash save failed: %s", e)

    def is_new(self, raw: str) -> bool:
        h   = hashlib.md5(raw[:200].encode(), usedforsecurity=False).hexdigest()
        now = time.time()
        with self._lock:
            if h in self._hashes and self._hashes[h] > now:
                return False
            self._hashes[h] = now + self.TTL
            self._save()
            return True

    def cleanup(self):
        """Evict expired hashes — call periodically."""
        now = time.time()
        with self._lock:
            before = len(self._hashes)
            self._hashes = {h: exp for h, exp in self._hashes.items()
                            if exp > now}
            removed = before - len(self._hashes)
            if removed:
                log.info("LogHashStore: evicted %d expired hashes", removed)
                self._save()


# ╔══════════════════════════════════════════════════════════════
# ║  CPU SAMPLER  (non-blocking background sample — Prompt CPU opt)
# ╚══════════════════════════════════════════════════════════════

class CpuSampler:
    """
    Samples cpu_percent(interval=None) in a background thread every 5 s,
    making it available instantly without blocking the check cycle.
    """
    def __init__(self, interval: float = 5.0):
        self._interval = interval
        self._value    = 0.0
        self._lock     = threading.Lock()
        # Prime the pump
        psutil.cpu_percent(interval=None)
        t = threading.Thread(target=self._loop, daemon=True, name="cpu-sampler")
        t.start()

    def _loop(self):
        while True:
            try:
                v = psutil.cpu_percent(interval=None)
                with self._lock:
                    self._value = v
            except Exception:
                pass
            time.sleep(self._interval)

    @property
    def value(self) -> float:
        with self._lock:
            return self._value


# ╔══════════════════════════════════════════════════════════════
# ║  CONTAINER STATUS CACHE  (Prompt CPU opt)
# ╚══════════════════════════════════════════════════════════════

class ContainerCache:
    """Caches `docker ps` output for CACHE_TTL seconds."""

    def __init__(self, ttl: int = CACHE_TTL):
        self._ttl      = ttl
        self._cache: Dict[str, str] = {}
        self._ts: float = 0.0
        self._lock = threading.Lock()

    def get(self) -> Dict[str, str]:
        with self._lock:
            if time.monotonic() - self._ts < self._ttl:
                return dict(self._cache)
        # Refresh outside lock to avoid blocking
        fresh = self._fetch()
        with self._lock:
            self._cache = fresh
            self._ts    = time.monotonic()
        return dict(fresh)

    @staticmethod
    def _fetch() -> Dict[str, str]:
        try:
            r = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
                capture_output=True, text=True, timeout=10,
            )
            result: Dict[str, str] = {}
            for line in r.stdout.strip().splitlines():
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    result[parts[0]] = parts[1]
            return result
        except (subprocess.TimeoutExpired, OSError):
            return {}


# ╔══════════════════════════════════════════════════════════════
# ║  PERF METRICS  (Prompt 6 / benchmarking)
# ╚══════════════════════════════════════════════════════════════

class PerfMetrics:
    """Optional per-check timing, slowest-check tracking."""

    def __init__(self, enabled: bool = False):
        self._enabled = enabled
        # {check_name: deque of elapsed seconds, maxlen=20}
        self._timings: Dict[str, Deque[float]] = {}
        self._lock = threading.Lock()

    def record(self, name: str, elapsed: float):
        if not self._enabled:
            return
        with self._lock:
            if name not in self._timings:
                self._timings[name] = deque(maxlen=20)
            self._timings[name].append(elapsed)

    def report(self) -> str:
        if not self._enabled or not self._timings:
            return ""
        with self._lock:
            rows = sorted(
                [(name, sum(v)/len(v), max(v))
                 for name, v in self._timings.items()],
                key=lambda x: x[1], reverse=True,
            )
        lines = ["<b>⏱ Perf Metrics (avg/max seconds)</b>"]
        for name, avg, mx in rows[:10]:
            lines.append(f"  {name:<30} avg={avg:.3f}s  max={mx:.3f}s")
        return "\n".join(lines)


# ╔══════════════════════════════════════════════════════════════
# ║  LOG BUNDLE SCHEDULER
# ╚══════════════════════════════════════════════════════════════

class LogBundleScheduler:
    def __init__(self, tg: Telegram, docker_cmd: Optional[List[str]]):
        self._tg         = tg
        self._docker_cmd = docker_cmd
        self._last_date: Optional[str] = None
        self._lock = threading.Lock()

    def tick(self):
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        target_minute  = LOG_BUNDLE_HOUR * 60 + LOG_BUNDLE_MINUTE
        current_minute = now.hour * 60 + now.minute
        if abs(current_minute - target_minute) > 1:
            return
        with self._lock:
            if self._last_date == today:
                return
            self._last_date = today
        log.info("Daily log bundle: starting collection for %s", today)
        try:
            zip_path = self._build_zip(today)
            self._send(zip_path, today)
            self._cleanup_old_archives()
        except OSError as e:
            log.exception("[MONITOR_ERR] Log bundle I/O error: %s", e)
            self._tg.send(
                f"❌ <b>Daily log bundle FAILED</b> on {HOSTNAME}\n{e}",
                priority=Priority.WARNING,
            )
        except Exception as e:
            log.exception("[MONITOR_ERR] Log bundle failed: %s", e)
            self._tg.send(
                f"❌ <b>Daily log bundle FAILED</b> on {HOSTNAME}\n{e}",
                priority=Priority.WARNING,
            )
        finally:
            # Prompt 6: explicit GC after heavy zip operation
            gc.collect()

    def build_zip_now(self, date_str: str) -> Path:
        return self._build_zip(date_str)

    def _build_zip(self, date_str: str) -> Path:
        zip_path  = LOG_ARCHIVE_DIR / f"pdfwala_logs_{date_str}.zip"
        collected = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=6) as zf:
            if LOG_FILE.exists():
                zf.write(LOG_FILE, "monitor/monitor.log")
                collected += 1
            if self._docker_cmd:
                for name in EXPECTED_CONTAINERS:
                    try:
                        # Stream via subprocess pipe instead of loading all into memory
                        proc = subprocess.Popen(
                            ["docker", "logs", "--tail=5000",
                             "--no-color", "--timestamps", name],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                            text=True,
                        )
                        chunks = []
                        assert proc.stdout is not None
                        for line in proc.stdout:
                            chunks.append(line)
                            if len(chunks) > 6000:
                                chunks = chunks[-5000:]
                        proc.wait(timeout=35)
                        content = "".join(chunks)
                        if content.strip():
                            zf.writestr(
                                f"containers/{name}.log",
                                content.encode("utf-8", errors="replace"),
                            )
                            collected += 1
                    except subprocess.TimeoutExpired:
                        zf.writestr(f"containers/{name}_ERROR.txt",
                                    "Timed out collecting logs")
                    except OSError as e:
                        zf.writestr(f"containers/{name}_ERROR.txt",
                                    f"Failed to collect: {e}")
            for nginx_log in [
                Path("/var/log/nginx/access.log"),
                Path("/var/log/nginx/error.log"),
                BASE_DIR / "nginx" / "logs" / "access.log",
                BASE_DIR / "nginx" / "logs" / "error.log",
            ]:
                if nginx_log.exists() and nginx_log.stat().st_size > 0:
                    try:
                        r = subprocess.run(
                            ["tail", "-n", "10000", str(nginx_log)],
                            capture_output=True, text=True, timeout=10,
                        )
                        if r.stdout.strip():
                            zf.writestr(
                                f"nginx/{nginx_log.name}",
                                r.stdout.encode("utf-8", errors="replace"),
                            )
                            collected += 1
                    except (subprocess.TimeoutExpired, OSError):
                        pass
            if METRICS_FILE.exists():
                zf.write(METRICS_FILE, "metrics/monitor_metrics.json")
            manifest = {
                "generated_at":    datetime.now(timezone.utc).isoformat(),
                "hostname":        HOSTNAME,
                "date":            date_str,
                "files_collected": collected,
            }
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        size_kb = zip_path.stat().st_size // 1024
        log.info("Log bundle built: %s (%d KB, %d sources)",
                 zip_path.name, size_kb, collected)
        gc.collect()  # Prompt 6: GC after heavy operation
        return zip_path

    TG_MAX_BYTES = 49 * 1024 * 1024

    def _send(self, zip_path: Path, date_str: str):
        size_bytes = zip_path.stat().st_size
        size_kb    = size_bytes // 1024
        if size_bytes > self.TG_MAX_BYTES:
            self._tg.send(
                f"⚠️ <b>Daily log bundle too large</b> on {HOSTNAME}\n"
                f"Full zip: {size_kb//1024} MB — sending errors-only instead.",
                priority=Priority.WARNING,
            )
            zip_path = self._build_errors_only_zip(date_str)
            size_kb  = zip_path.stat().st_size // 1024
        caption = (
            f"📦 <b>PDFWala Daily Logs — {date_str}</b>\n"
            f"🖥️ {HOSTNAME}\n"
            f"📁 {zip_path.name}  ({size_kb} KB)\n"
            f"⏰ Generated: {datetime.now().strftime('%H:%M:%S')}"
        )
        ok = self._tg.send_document(zip_path, caption=caption)
        if not ok:
            self._tg.send(
                f"❌ <b>Daily log send FAILED</b> on {HOSTNAME} ({date_str})",
                priority=Priority.WARNING,
            )

    def _build_errors_only_zip(self, date_str: str) -> Path:
        zip_path = LOG_ARCHIVE_DIR / f"pdfwala_errors_{date_str}.zip"
        patterns = ["ERROR", "CRITICAL", "FATAL", "Exception", "Traceback",
                    "500", "502", "503", "OOM", "Killed", "circuit breaker"]
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED,
                             compresslevel=9) as zf:
            for name in EXPECTED_CONTAINERS:
                try:
                    r = subprocess.run(
                        ["docker", "logs", "--tail=10000", "--no-color", name],
                        capture_output=True, text=True, timeout=30,
                    )
                    all_lines   = ((r.stdout or "") + (r.stderr or "")).splitlines()
                    error_lines = [
                        ln for ln in all_lines
                        if any(p.lower() in ln.lower() for p in patterns)
                    ]
                    if error_lines:
                        content = "\n".join(error_lines[-5000:])
                        zf.writestr(
                            f"errors/{name}_errors.log",
                            content.encode("utf-8", errors="replace"),
                        )
                except subprocess.TimeoutExpired:
                    zf.writestr(f"errors/{name}_ERROR.txt", "Timed out")
                except OSError as e:
                    zf.writestr(f"errors/{name}_ERROR.txt", f"Failed: {e}")
            if LOG_FILE.exists():
                try:
                    r = subprocess.run(["tail", "-n", "2000", str(LOG_FILE)],
                                       capture_output=True, text=True,
                                       timeout=10)
                    if r.stdout:
                        zf.writestr("monitor/monitor_tail.log",
                                    r.stdout.encode("utf-8", errors="replace"))
                except (subprocess.TimeoutExpired, OSError):
                    pass
            zf.writestr("manifest.json", json.dumps({
                "type":         "errors_only_fallback",
                "date":         date_str,
                "hostname":     HOSTNAME,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }, indent=2))
        log.info("Errors-only zip: %s (%d KB)",
                 zip_path.name, zip_path.stat().st_size // 1024)
        gc.collect()
        return zip_path

    def _cleanup_old_archives(self):
        cutoff = datetime.now() - timedelta(days=LOG_ARCHIVE_DAYS)
        removed = 0
        for f in LOG_ARCHIVE_DIR.glob("pdfwala_logs_*.zip"):
            try:
                date_part = f.stem.replace("pdfwala_logs_", "")
                file_date = datetime.strptime(date_part, "%Y-%m-%d")
                if file_date < cutoff:
                    f.unlink()
                    removed += 1
            except (ValueError, OSError):
                pass
        if removed:
            log.info("Cleaned up %d old log archive(s)", removed)


# ╔══════════════════════════════════════════════════════════════
# ║  RUNTIME STATE
# ╚══════════════════════════════════════════════════════════════

class RuntimeState:
    def __init__(self):
        self._lock          = threading.Lock()
        self.cpu_threshold  = CPU_THRESHOLD
        self.ram_threshold  = RAM_THRESHOLD
        self.muted_until: Optional[datetime] = None
        self._pending: Dict[str, dict] = {}

    def is_muted(self) -> bool:
        with self._lock:
            if self.muted_until is None:
                return False
            if datetime.now(timezone.utc) >= self.muted_until:
                self.muted_until = None
                return False
            return True

    def mute(self, minutes: int):
        with self._lock:
            self.muted_until = (datetime.now(timezone.utc)
                                + timedelta(minutes=minutes))

    def unmute(self):
        with self._lock:
            self.muted_until = None

    def set_pending(self, chat_id: str, cmd: str):
        with self._lock:
            self._pending[chat_id] = {
                "cmd":     cmd,
                "expires": datetime.now(timezone.utc) + timedelta(seconds=60),
            }

    def pop_pending(self, chat_id: str) -> Optional[str]:
        with self._lock:
            p = self._pending.pop(chat_id, None)
            if p and datetime.now(timezone.utc) < p["expires"]:
                return p["cmd"]
            return None

    def has_pending(self, chat_id: str) -> bool:
        with self._lock:
            p = self._pending.get(chat_id)
            if p and datetime.now(timezone.utc) < p["expires"]:
                return True
            self._pending.pop(chat_id, None)
            return False


# ╔══════════════════════════════════════════════════════════════
# ║  TELEGRAM COMMAND HANDLER
# ╚══════════════════════════════════════════════════════════════

class TelegramCommandHandler:
    def __init__(self, tg: Telegram, state: RuntimeState,
                 log_bundle: LogBundleScheduler):
        self._tg          = tg
        self._state       = state
        self._log_bundle  = log_bundle
        self._offset      = 0
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="tg-cmd-handler"
        )
        self._thread.start()
        log.info("TelegramCommandHandler started (poll every %ds)",
                 CMD_POLL_INTERVAL)

    def _poll_loop(self):
        while True:
            try:
                updates = self._tg.get_updates(self._offset, timeout=2)
                for upd in updates:
                    self._offset = upd["update_id"] + 1
                    self._handle_update(upd)
            except Exception as e:
                log.debug("[CMD_ERR] Command poll error: %s", e)
            time.sleep(CMD_POLL_INTERVAL)

    def _handle_update(self, upd: dict):
        msg = upd.get("message") or upd.get("edited_message")
        if not msg:
            return
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text    = (msg.get("text") or "").strip()
        if not text or not chat_id:
            return
        if chat_id != CHAT_ID:
            log.warning("[PERM_ERR] Rejected command from unknown chat_id=%s",
                        chat_id)
            self._reply(chat_id, "⛔ Unauthorized.")
            return
        log.info("Command from chat_id=%s: %s", chat_id, text[:80])
        parts = text.split()
        cmd   = parts[0].lower().lstrip("/").split("@")[0]
        args  = parts[1:]
        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler:
            try:
                handler(chat_id, args)
            except CommandError as e:
                log.error("[CMD_ERR] Command %s failed: %s", cmd, e)
                self._reply(chat_id, f"⚠️ Command error: {e}")
            except subprocess.TimeoutExpired:
                log.error("[TIMEOUT_ERR] Command %s timed out", cmd)
                self._reply(chat_id, "⏱ Command timed out.")
            except Exception as e:
                log.exception("[CMD_ERR] Command %s unexpected error", cmd)
                self._reply(chat_id, f"⚠️ Command failed: {e}")
        else:
            self._reply(chat_id, "❓ Unknown command. Try /status /help")

    def _reply(self, chat_id: str, text: str):
        self._tg.send(text, chat_id=chat_id, priority=Priority.INFO)

    # ── helpers ───────────────────────────────────────────────

    @staticmethod
    def _run(cmd: List[str], timeout: int = 8) -> Tuple[int, str]:
        try:
            r = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout
            )
            return r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            return -1, "⏱ Command timed out"
        except PermissionError as e:
            return -3, f"[PERM_ERR] {e}"
        except OSError as e:
            return -2, str(e)

    @staticmethod
    def _docker_logs(container: str, tail: int = 100) -> str:
        rc, out = TelegramCommandHandler._run(
            ["docker", "logs", "--tail", str(tail), "--no-color",
             "--timestamps", container],
            timeout=10,
        )
        if rc not in (0, 1):
            return f"⚠️ Failed to get logs: {out[:200]}"
        return out or "(no output)"

    @staticmethod
    def _redis_cmd(*args: str) -> str:
        rc, out = TelegramCommandHandler._run(
            ["docker", "exec", REDIS_CONTAINER, "redis-cli"] + list(args),
            timeout=6,
        )
        return out.strip() if rc == 0 else f"err:{out.strip()[:100]}"

    @staticmethod
    def _folder_size(path: Path) -> str:
        if not path.exists():
            return "N/A"
        rc, out = TelegramCommandHandler._run(
            ["du", "-sh", str(path)], timeout=10
        )
        if rc == 0 and out.strip():
            return out.split()[0]
        return "err"

    @staticmethod
    def _nginx_log_path() -> Optional[Path]:
        candidates = [
            Path("/var/log/nginx/access.log"),
            BASE_DIR / "nginx" / "logs" / "access.log",
        ]
        for p in candidates:
            if p.exists():
                return p
        return None

    # ── commands ──────────────────────────────────────────────

    def _cmd_help(self, chat_id: str, args: List[str]):
        self._reply(chat_id, (
            "<b>📋 PDFWala Bot Commands</b>\n\n"
            "<b>Real-time</b>\n"
            "  /status  /errors  /disk  /queues\n"
            "  /users  /tools  /jobs  /ping  /version\n\n"
            "<b>Logs</b>\n"
            "  /logs &lt;app|worker|nginx|office|slow&gt;\n"
            "  /errors full  /download_logs\n\n"
            "<b>Control</b> (need /confirm yes)\n"
            "  /restart &lt;container&gt;\n"
            "  /clear_temp  /clear_outputs  /docker_prune\n\n"
            "<b>Alerts</b>\n"
            "  /mute &lt;30m|1h|2h&gt;  /unmute\n"
            "  /threshold &lt;cpu|ram&gt; &lt;value&gt;\n"
            "  /settings\n\n"
            "<b>Analytics</b>\n"
            "  /stats &lt;today|week&gt;  /top_files  /slowest\n\n"
            "<b>Security</b>\n"
            "  /auth_errors  /rate_limits  /suspicious\n"
        ))

    def _cmd_status(self, chat_id: str, args: List[str]):
        self._reply(chat_id, "⏳ Gathering status…")
        lines = [f"<b>📊 Status — {HOSTNAME}</b>",
                 f"<code>{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</code>", ""]
        try:
            cpu  = psutil.cpu_percent(interval=None)  # non-blocking
            ram  = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            swap = psutil.swap_memory()
            l1, _, _ = os.getloadavg()
            lines += [
                "<b>⚙️ System</b>",
                f"  CPU  : {cpu:.1f}%",
                f"  RAM  : {ram.percent:.1f}%  "
                f"({ram.used/1e9:.1f}/{ram.total/1e9:.1f} GB)",
                f"  Disk : {disk.percent:.1f}%  (free {disk.free/1e9:.1f} GB)",
                f"  Swap : {swap.percent:.1f}%",
                f"  Load : {l1:.2f}",
                "",
            ]
        except psutil.Error as e:
            lines.append(f"⚠️ System metrics error: {e}")

        rc, out = self._run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
            timeout=8,
        )
        running: Dict[str, str] = {}
        if rc == 0:
            for line in out.strip().splitlines():
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    running[parts[0]] = parts[1]
        lines.append("<b>🐳 Containers</b>")
        for name in EXPECTED_CONTAINERS:
            label = name.replace("pdfwala-", "")
            if name in running:
                st   = running[name]
                icon = "✅" if "Up" in st else "❌"
                lines.append(f"  {icon} {label}: {st[:40]}")
            else:
                lines.append(f"  ❌ {label}: MISSING")

        lines.append("")
        lines.append("<b>🔴 Redis</b>")
        pong = self._redis_cmd("ping")
        lines.append(
            f"  {'✅ PONG' if pong.upper() == 'PONG' else '❌ ' + pong}")

        if self._state.is_muted():
            muted_until = self._state.muted_until
            lines.append(
                f"\n🔕 <b>Alerts muted until "
                f"{muted_until.strftime('%H:%M')} UTC</b>")
        self._reply(chat_id, "\n".join(lines))

    def _cmd_errors(self, chat_id: str, args: List[str]):
        if args and args[0].lower() == "full":
            self._cmd_errors_full(chat_id)
            return
        patterns = ["ERROR", "CRITICAL", "Exception", "Traceback",
                    "FATAL", "500", "502", "503", "OOM", "Killed",
                    "circuit breaker"]
        found: List[str] = []
        for name in EXPECTED_CONTAINERS:
            label = name.replace("pdfwala-", "")
            rc, out = self._run(
                ["docker", "logs", "--tail=200", "--no-color", name],
                timeout=8,
            )
            if rc not in (0, 1):
                continue
            # Python string ops instead of grep (Prompt CPU opt)
            for line in out.splitlines():
                ll = line.lower()
                if any(p.lower() in ll for p in patterns):
                    found.append(f"[{label}] {line[:160]}")
        if not found:
            self._reply(chat_id, "✅ No recent errors found in container logs.")
            return
        preview = "\n".join(found[-10:])
        self._reply(chat_id,
            f"<b>🔴 Last {min(10,len(found))} error(s)  "
            f"(total {len(found)} found)</b>\n"
            f"<pre>{preview}</pre>"
        )

    def _cmd_errors_full(self, chat_id: str):
        self._reply(chat_id, "⏳ Scanning last 24h of logs for errors…")
        patterns = ["ERROR", "CRITICAL", "Exception", "Traceback",
                    "FATAL", "500", "502", "503", "OOM", "Killed",
                    "circuit breaker", "rate limit exceeded", "pdf corrupt"]
        all_errors: List[str] = []
        for name in EXPECTED_CONTAINERS:
            label = name.replace("pdfwala-", "")
            rc, out = self._run(
                ["docker", "logs", "--since=24h", "--no-color", name],
                timeout=20,
            )
            for line in out.splitlines():
                ll = line.lower()
                if any(p.lower() in ll for p in patterns):
                    all_errors.append(f"[{label}] {line[:160]}")
        if not all_errors:
            self._reply(chat_id, "✅ No errors in the last 24 hours.")
            return
        chunk: List[str] = []
        chunk_len = 0
        self._reply(chat_id, f"<b>🔴 {len(all_errors)} error(s) in last 24h</b>")
        for line in all_errors:
            if chunk_len + len(line) > 3400:
                self._reply(chat_id, f"<pre>{''.join(chunk)}</pre>")
                chunk = []
                chunk_len = 0
            chunk.append(line + "\n")
            chunk_len += len(line) + 1
        if chunk:
            self._reply(chat_id, f"<pre>{''.join(chunk)}</pre>")

    def _cmd_disk(self, chat_id: str, args: List[str]):
        lines = [f"<b>💾 Disk Usage — {HOSTNAME}</b>", ""]
        try:
            d = psutil.disk_usage("/")
            lines.append(
                f"  /             {d.used/1e9:.1f}/"
                f"{d.total/1e9:.1f} GB  ({d.percent:.0f}%)"
            )
        except psutil.Error:
            lines.append("  / : error")
        for label, path in [
            ("uploads", UPLOADS_DIR),
            ("outputs", OUTPUTS_DIR),
            ("temp",    TEMP_DIR),
        ]:
            size = self._folder_size(path)
            lines.append(f"  {label:<13} {size}")
        rc, out = self._run(["du", "-sh", "/var/lib/docker"], timeout=15)
        docker_size = out.split()[0] if rc == 0 and out.strip() else "N/A"
        lines.append(f"  docker        {docker_size}")
        self._reply(chat_id, "\n".join(lines))

    def _cmd_queues(self, chat_id: str, args: List[str]):
        lines = ["<b>📬 Celery Queue Depths</b>"]
        for q in CELERY_QUEUES:
            depth = self._redis_cmd("LLEN", q)
            try:
                d    = int(depth)
                icon = "🟢" if d < CELERY_QUEUE_WARN else (
                    "🟡" if d < CELERY_QUEUE_CRIT else "🔴")
                lines.append(f"  {icon} {q}: {d} jobs")
            except ValueError:
                lines.append(f"  ❓ {q}: {depth}")
        self._reply(chat_id, "\n".join(lines))

    def _cmd_users(self, chat_id: str, args: List[str]):
        log_path = self._nginx_log_path()
        if not log_path:
            self._reply(chat_id, "⚠️ Nginx access log not found.")
            return
        # Python string ops on limited tail (Prompt CPU opt)
        try:
            lines_raw = log_path.read_text(errors="replace").splitlines()
            tail      = lines_raw[-NGINX_TAIL_LINES:]
            ips       = {ln.split()[0] for ln in tail if ln.strip()}
            self._reply(chat_id,
                f"<b>👥 Unique IPs (last {NGINX_TAIL_LINES} lines)</b>\n"
                f"  Unique IPs  : {len(ips)}\n"
                f"  Lines scanned: {len(tail)}\n"
                f"  <i>Log: {log_path}</i>"
            )
        except OSError as e:
            self._reply(chat_id, f"⚠️ Could not read nginx log: {e}")

    def _cmd_tools(self, chat_id: str, args: List[str]):
        log_path = self._nginx_log_path()
        if not log_path:
            self._reply(chat_id, "⚠️ Nginx access log not found.")
            return
        today = datetime.now().strftime("%d/%b/%Y")
        try:
            raw_lines = log_path.read_text(errors="replace").splitlines()
            tail      = raw_lines[-NGINX_TAIL_LINES:]
            counter: Dict[str, int] = {}
            for ln in tail:
                if today not in ln:
                    continue
                parts = ln.split()
                if len(parts) >= 7:
                    ep = parts[6].split("?")[0]
                    counter[ep] = counter.get(ep, 0) + 1
            if not counter:
                self._reply(chat_id, "⚠️ No entries found for today.")
                return
            top5  = sorted(counter.items(), key=lambda x: x[1], reverse=True)[:5]
            lines = [f"<b>🔧 Top 5 Endpoints Today ({today})</b>"]
            for ep, cnt in top5:
                lines.append(f"  {cnt:>6}×  {ep}")
            self._reply(chat_id, "\n".join(lines))
        except OSError as e:
            self._reply(chat_id, f"⚠️ Could not read nginx log: {e}")

    def _cmd_jobs(self, chat_id: str, args: List[str]):
        rc, out = self._run(
            ["docker", "exec", REDIS_CONTAINER, "redis-cli", "KEYS", "job:*"],
            timeout=8,
        )
        if rc != 0:
            self._reply(chat_id, f"⚠️ Redis error: {out[:100]}")
            return
        keys = [k for k in out.strip().splitlines() if k.startswith("job:")]
        queue_info = []
        for q in CELERY_QUEUES:
            depth = self._redis_cmd("LLEN", q)
            queue_info.append(f"  {q}: {depth}")
        self._reply(chat_id,
            f"<b>⚙️ Job Status</b>\n"
            f"  Redis job:* keys : {len(keys)}\n\n"
            f"<b>📬 Queue depths</b>\n" + "\n".join(queue_info)
        )

    def _cmd_ping(self, chat_id: str, args: List[str]):
        lines = ["<b>🏓 Endpoint Ping</b>"]
        hdrs  = {"User-Agent": "PDFWala-Monitor/5.0"}
        for name, url, expected, _ in ENDPOINTS:
            try:
                t0   = time.monotonic()
                resp = requests.get(url, timeout=10, headers=hdrs,
                                    allow_redirects=True)
                ms   = (time.monotonic() - t0) * 1000
                icon = "✅" if resp.status_code == expected else "❌"
                lines.append(f"  {icon} {name}: {resp.status_code}  {ms:.0f}ms")
            except requests.exceptions.Timeout:
                lines.append(f"  ❌ {name}: TIMEOUT")
            except requests.exceptions.RequestException as e:
                lines.append(f"  ❌ {name}: {str(e)[:60]}")
        self._reply(chat_id, "\n".join(lines))

    def _cmd_version(self, chat_id: str, args: List[str]):
        version = "unknown"
        try:
            r = requests.get("https://npkpadala.com/health", timeout=8,
                             headers={"User-Agent": "PDFWala-Monitor/5.0"})
            if r.status_code == 200:
                data    = r.json()
                version = (data.get("version") or
                           data.get("app_version") or "unknown")
        except requests.exceptions.RequestException:
            pass
        uptime_s  = int(time.time() - psutil.boot_time())
        uptime_h  = uptime_s // 3600
        uptime_d  = uptime_h // 24
        uptime_hr = uptime_h % 24
        uptime_m  = (uptime_s % 3600) // 60
        self._reply(chat_id,
            f"<b>ℹ️ Version &amp; Uptime</b>\n"
            f"  App version   : <code>{version}</code>\n"
            f"  Monitor       : v5\n"
            f"  Server uptime : {uptime_d}d {uptime_hr}h {uptime_m}m\n"
            f"  Hostname      : {HOSTNAME}"
        )

    def _cmd_logs(self, chat_id: str, args: List[str]):
        if not args:
            self._reply(chat_id,
                "Usage: /logs &lt;app|worker|nginx|office|slow&gt;")
            return
        alias = args[0].lower()
        name  = CONTAINER_ALIASES.get(alias)
        if not name:
            self._reply(chat_id,
                f"⚠️ Unknown container alias: <code>{alias}</code>\n"
                f"Valid: {', '.join(CONTAINER_ALIASES.keys())}")
            return
        self._reply(chat_id,
            f"⏳ Fetching last 100 lines of <b>{name}</b>…")
        out = self._docker_logs(name, tail=100)
        if len(out) > 3400:
            out = "…[truncated]\n" + out[-3400:]
        self._reply(chat_id, f"<b>📄 {name}</b>\n<pre>{out}</pre>")

    def _cmd_download_logs(self, chat_id: str, args: List[str]):
        self._reply(chat_id, "⏳ Building log ZIP — this may take ~30s…")
        try:
            date_str = datetime.now().strftime("%Y-%m-%d")
            zip_path = self._log_bundle.build_zip_now(date_str)
            size_kb  = zip_path.stat().st_size // 1024
            caption  = (
                f"📦 <b>On-demand log bundle</b>\n"
                f"🖥️ {HOSTNAME}\n"
                f"📁 {zip_path.name} ({size_kb} KB)\n"
                f"⏰ {datetime.now().strftime('%H:%M:%S')}"
            )
            ok = self._tg.send_document(zip_path, caption=caption)
            if not ok:
                self._reply(chat_id, "❌ Failed to send ZIP to Telegram.")
        except OSError as e:
            self._reply(chat_id, f"❌ Log bundle I/O failed: {e}")
        except Exception as e:
            self._reply(chat_id, f"❌ Log bundle failed: {e}")

    def _cmd_confirm(self, chat_id: str, args: List[str]):
        if not (args and args[0].lower() == "yes"):
            self._reply(chat_id, "Usage: /confirm yes")
            return
        cmd = self._state.pop_pending(chat_id)
        if not cmd:
            self._reply(chat_id,
                "⚠️ No pending command to confirm (expired?).")
            return
        log.info("Confirmed command from %s: %s", chat_id, cmd)
        self._execute_confirmed(chat_id, cmd)

    def _execute_confirmed(self, chat_id: str, cmd: str):
        parts  = cmd.split()
        action = parts[0]
        if action == "restart" and len(parts) > 1:
            alias = parts[1].lower()
            name  = CONTAINER_ALIASES.get(alias, alias)
            self._reply(chat_id, f"🔄 Restarting <b>{name}</b>…")
            rc, out = self._run(["docker", "restart", name], timeout=30)
            if rc == 0:
                self._reply(chat_id, f"✅ <b>{name}</b> restarted.")
            else:
                self._reply(chat_id,
                    f"❌ Restart failed:\n<pre>{out[:500]}</pre>")
        elif action == "clear_temp":
            self._reply(chat_id, "🗑 Clearing temp folder…")
            rc, out = self._run(
                ["bash", "-c", f"rm -rf {TEMP_DIR}/* && echo OK"], timeout=15)
            self._reply(chat_id,
                "✅ Temp folder cleared." if "OK" in out
                else f"❌ Failed: {out[:200]}")
        elif action == "clear_outputs":
            self._reply(chat_id, "🗑 Clearing outputs folder…")
            rc, out = self._run(
                ["bash", "-c", f"rm -rf {OUTPUTS_DIR}/* && echo OK"],
                timeout=15)
            self._reply(chat_id,
                "✅ Outputs folder cleared." if "OK" in out
                else f"❌ Failed: {out[:200]}")
        elif action == "docker_prune":
            self._reply(chat_id, "🧹 Running docker system prune -af…")
            rc, out = self._run(
                ["docker", "system", "prune", "-af"], timeout=120)
            if rc == 0:
                freed = "unknown"
                for line in out.splitlines():
                    if "Total reclaimed" in line:
                        freed = line.strip()
                        break
                self._reply(chat_id,
                    f"✅ Docker pruned.\n<code>{freed}</code>")
            else:
                self._reply(chat_id,
                    f"❌ Prune failed:\n<pre>{out[:400]}</pre>")

    def _cmd_restart(self, chat_id: str, args: List[str]):
        if not args:
            self._reply(chat_id,
                "Usage: /restart &lt;app|worker|nginx|office|slow|redis&gt;")
            return
        alias = args[0].lower()
        name  = CONTAINER_ALIASES.get(alias, alias)
        self._state.set_pending(chat_id, f"restart {alias}")
        self._reply(chat_id,
            f"⚠️ About to restart <b>{name}</b>.\n"
            f"Type /confirm yes within 60s to proceed.")

    def _cmd_clear_temp(self, chat_id: str, args: List[str]):
        self._state.set_pending(chat_id, "clear_temp")
        self._reply(chat_id,
            f"⚠️ About to delete all files in <code>{TEMP_DIR}</code>.\n"
            f"Type /confirm yes within 60s to proceed.")

    def _cmd_clear_outputs(self, chat_id: str, args: List[str]):
        self._state.set_pending(chat_id, "clear_outputs")
        self._reply(chat_id,
            f"⚠️ About to delete all files in <code>{OUTPUTS_DIR}</code>.\n"
            f"Type /confirm yes within 60s to proceed.")

    def _cmd_docker_prune(self, chat_id: str, args: List[str]):
        self._state.set_pending(chat_id, "docker_prune")
        self._reply(chat_id,
            "⚠️ About to run <code>docker system prune -af</code>.\n"
            "This removes ALL unused containers, images, and volumes.\n"
            "Type /confirm yes within 60s to proceed.")

    def _cmd_mute(self, chat_id: str, args: List[str]):
        if not args:
            self._reply(chat_id, "Usage: /mute &lt;30m|1h|2h&gt;")
            return
        arg  = args[0].lower()
        mins = {"30m": 30, "1h": 60, "2h": 120}.get(arg)
        if mins is None:
            m = re.match(r"^(\d+)m?$", arg)
            if m:
                mins = int(m.group(1))
            else:
                self._reply(chat_id,
                    "⚠️ Invalid duration. Use 30m, 1h, 2h, or <N>m")
                return
        self._state.mute(mins)
        until = self._state.muted_until
        self._reply(chat_id,
            f"🔕 Alerts muted for {mins} min "
            f"(until {until.strftime('%H:%M')} UTC)")

    def _cmd_unmute(self, chat_id: str, args: List[str]):
        self._state.unmute()
        self._reply(chat_id, "🔔 Alerts resumed.")

    def _cmd_threshold(self, chat_id: str, args: List[str]):
        if len(args) < 2:
            self._reply(chat_id,
                "Usage: /threshold &lt;cpu|ram&gt; &lt;value&gt;")
            return
        metric = args[0].lower()
        try:
            val = int(args[1])
            assert 1 <= val <= 100
        except (ValueError, AssertionError):
            self._reply(chat_id, "⚠️ Value must be 1-100")
            return
        if metric == "cpu":
            self._state.cpu_threshold = val
            CFG.set("CPU_THRESHOLD", val, source="telegram")
            self._reply(chat_id, f"✅ CPU alert threshold set to {val}%")
        elif metric == "ram":
            self._state.ram_threshold = val
            CFG.set("RAM_THRESHOLD", val, source="telegram")
            self._reply(chat_id, f"✅ RAM alert threshold set to {val}%")
        else:
            self._reply(chat_id, "⚠️ Unknown metric. Use cpu or ram")

    def _cmd_settings(self, chat_id: str, args: List[str]):
        mute_str = "off"
        if self._state.is_muted() and self._state.muted_until:
            mute_str = (f"until "
                        f"{self._state.muted_until.strftime('%H:%M')} UTC")
        change_log = CFG.change_log()
        change_str = (("\n  " + "\n  ".join(change_log[-3:]))
                      if change_log else " none")
        self._reply(chat_id,
            f"<b>⚙️ Current Settings</b>\n"
            f"  CPU threshold : {self._state.cpu_threshold}%\n"
            f"  RAM threshold : {self._state.ram_threshold}%\n"
            f"  Disk threshold: {DISK_THRESHOLD}%\n"
            f"  Swap threshold: {SWAP_THRESHOLD}%\n"
            f"  Load multiplier: ×{LOAD_MULTIPLIER}\n"
            f"  Alert cooldown: {ALERT_COOLDOWN}s\n"
            f"  Check interval: {CHECK_INTERVAL}s\n"
            f"  Mute status   : {mute_str}\n"
            f"  Celery warn   : {CELERY_QUEUE_WARN} jobs\n"
            f"  Celery crit   : {CELERY_QUEUE_CRIT} jobs\n"
            f"  SSL warn      : {SSL_WARN_DAYS}d\n"
            f"  Log bundle    : {LOG_BUNDLE_HOUR:02d}:{LOG_BUNDLE_MINUTE:02d}\n"
            f"  Log tail lines: {LOG_TAIL_LINES}\n"
            f"  Container cache TTL: {CACHE_TTL}s\n"
            f"  Perf metrics  : {ENABLE_PERF_METRICS}\n"
            f"  Mem warn (MB) : {MEMORY_WARN_THRESHOLD_MB}\n"
            f"<b>Recent config changes:</b>{change_str}"
        )

    # ── analytics ─────────────────────────────────────────────

    def _cmd_stats(self, chat_id: str, args: List[str]):
        period = args[0].lower() if args else "today"
        if period == "today":
            date_str = datetime.now().strftime("%d/%b/%Y")
            label    = "Today"
            days     = [date_str]
        elif period == "week":
            label = "Last 7 days"
            days  = [
                (datetime.now() - timedelta(days=i)).strftime("%d/%b/%Y")
                for i in range(7)
            ]
        else:
            self._reply(chat_id, "Usage: /stats &lt;today|week&gt;")
            return
        self._reply(chat_id, f"⏳ Parsing nginx logs for {label}…")
        log_path = self._nginx_log_path()
        if not log_path:
            self._reply(chat_id, "⚠️ Nginx access log not found.")
            return
        total_req = 0
        all_ips: Set[str] = set()
        total_errors = 0
        try:
            raw_lines = log_path.read_text(errors="replace").splitlines()
            tail      = raw_lines[-NGINX_TAIL_LINES:]
            for ln in tail:
                if not any(d in ln for d in days):
                    continue
                parts = ln.split()
                if parts:
                    all_ips.add(parts[0])
                if len(parts) >= 9:
                    try:
                        if int(parts[8]) >= 400:
                            total_errors += 1
                    except ValueError:
                        pass
                total_req += 1
        except OSError as e:
            self._reply(chat_id, f"⚠️ Could not read nginx log: {e}")
            return
        self._reply(chat_id,
            f"<b>📈 Stats — {label}</b>\n"
            f"  Total requests  : {total_req}\n"
            f"  Unique IPs      : {len(all_ips)}\n"
            f"  Error responses : {total_errors}"
        )

    def _cmd_top_files(self, chat_id: str, args: List[str]):
        lines = ["<b>📂 Top 5 Largest Files (uploads + outputs)</b>"]
        for folder in [UPLOADS_DIR, OUTPUTS_DIR]:
            if not folder.exists():
                continue
            rc, out = self._run(
                ["bash", "-c",
                 f"find {folder} -type f -printf '%s %p\\n' | "
                 "sort -rn | head -5"],
                timeout=12,
            )
            if rc == 0 and out.strip():
                lines.append(f"\n<b>{folder.name}/</b>")
                for entry in out.strip().splitlines():
                    parts = entry.split(None, 1)
                    if len(parts) == 2:
                        size_mb = int(parts[0]) / (1024 * 1024)
                        fname   = Path(parts[1]).name
                        lines.append(f"  {size_mb:6.1f} MB  {fname}")
        self._reply(chat_id, "\n".join(lines))

    def _cmd_slowest(self, chat_id: str, args: List[str]):
        self._reply(chat_id, "⏳ Scanning app logs for slow requests…")
        rc, out = self._run(
            ["docker", "logs", "--tail=2000", "--no-color", "pdfwala-app"],
            timeout=15,
        )
        if rc not in (0, 1):
            self._reply(chat_id, "⚠️ Could not read app logs.")
            return
        timing_re  = re.compile(
            r"(GET|POST|PUT|DELETE)\s+(\S+)\s+\d{3}\s+(\d+)ms", re.IGNORECASE
        )
        endpoints: List[Tuple[int, str]] = []
        for line in out.splitlines():
            m = timing_re.search(line)
            if m:
                try:
                    ms = int(m.group(3))
                    ep = f"{m.group(1)} {m.group(2)[:60]}"
                    endpoints.append((ms, ep))
                except ValueError:
                    pass
        if not endpoints:
            self._reply(chat_id,
                "ℹ️ No timing data found. "
                "(Requires logs in format: METHOD /path CODE <N>ms)")
            return
        endpoints.sort(reverse=True)
        top5  = endpoints[:5]
        lines = ["<b>🐢 Top 5 Slowest Requests (recent logs)</b>"]
        for ms, ep in top5:
            lines.append(f"  {ms:6}ms  {ep}")
        self._reply(chat_id, "\n".join(lines))

    # ── security ──────────────────────────────────────────────

    def _count_nginx_status(self, status_code: str) -> Tuple[int, str]:
        log_path = self._nginx_log_path()
        if not log_path:
            return 0, "log not found"
        today = datetime.now().strftime("%d/%b/%Y")
        try:
            raw_lines = log_path.read_text(errors="replace").splitlines()
            tail      = raw_lines[-NGINX_TAIL_LINES:]
            count     = sum(
                1 for ln in tail
                if today in ln and len(ln.split()) >= 9
                and ln.split()[8] == status_code
            )
            return count, ""
        except OSError as e:
            return 0, str(e)

    def _cmd_auth_errors(self, chat_id: str, args: List[str]):
        count, err = self._count_nginx_status("401")
        if err:
            self._reply(chat_id, f"⚠️ {err}")
            return
        icon = "🔴" if count > 50 else ("🟡" if count > 10 else "🟢")
        self._reply(chat_id,
            f"<b>🔐 Auth Errors (401) Today</b>\n"
            f"  {icon} Count: <b>{count}</b>")

    def _cmd_rate_limits(self, chat_id: str, args: List[str]):
        count, err = self._count_nginx_status("429")
        if err:
            self._reply(chat_id, f"⚠️ {err}")
            return
        icon = "🔴" if count > 100 else ("🟡" if count > 20 else "🟢")
        self._reply(chat_id,
            f"<b>🚦 Rate Limited (429) Today</b>\n"
            f"  {icon} Count: <b>{count}</b>")

    def _cmd_suspicious(self, chat_id: str, args: List[str]):
        log_path = self._nginx_log_path()
        if not log_path:
            self._reply(chat_id, "⚠️ Nginx access log not found.")
            return
        self._reply(chat_id,
            "⏳ Checking for high-volume IPs in recent logs…")
        try:
            raw_lines = log_path.read_text(errors="replace").splitlines()
            tail      = raw_lines[-NGINX_TAIL_LINES:]
            ip_counts: Dict[str, int] = {}
            for ln in tail:
                parts = ln.split()
                if parts:
                    ip = parts[0]
                    ip_counts[ip] = ip_counts.get(ip, 0) + 1
            suspicious = sorted(
                [(cnt, ip) for ip, cnt in ip_counts.items() if cnt > 100],
                reverse=True,
            )[:10]
            if not suspicious:
                self._reply(chat_id,
                    "✅ No IPs with >100 requests in recent logs.")
                return
            lines = [
                "<b>🔍 Suspicious IPs (&gt;100 req in recent logs)</b>"]
            for cnt, ip in suspicious:
                lines.append(f"  {cnt:>6}×  {ip}")
            self._reply(chat_id, "\n".join(lines))
        except OSError as e:
            self._reply(chat_id, f"⚠️ Could not read nginx log: {e}")


# ╔══════════════════════════════════════════════════════════════
# ║  MAIN MONITOR
# ╚══════════════════════════════════════════════════════════════

class Monitor:

    def __init__(self):
        self.running    = True
        self._stop      = threading.Event()
        self.tg         = Telegram(TOKEN, CHAT_ID)
        self.cd         = Cooldown(ALERT_COOLDOWN)
        self.restart_cd = Cooldown(AUTO_RESTART_COOLDOWN)
        self.log_store  = LogHashStore(LOG_HASH_FILE)
        self.docker_cmd = self._find_docker_compose()

        self._mem_samples: Deque[float] = deque(maxlen=MEM_TREND_CYCLES + 1)
        self._prev_io: Optional[object] = None
        self._prev_io_ts: float = 0.0

        # Perf metrics (Prompt 6)
        self._perf = PerfMetrics(enabled=bool(ENABLE_PERF_METRICS))

        # CPU sampler (non-blocking — Prompt CPU opt)
        self._cpu_sampler = CpuSampler(interval=5.0)

        # Container cache (Prompt CPU opt)
        self._ct_cache = ContainerCache(ttl=int(CACHE_TTL))

        # Metrics sliding window — bounded deque (Prompt 6)
        self._metrics_history: Deque[dict] = deque(maxlen=360)  # 6h at 1/min

        self.state      = RuntimeState()
        self.log_bundle = LogBundleScheduler(self.tg, self.docker_cmd)
        self.cmd_handler = TelegramCommandHandler(
            self.tg, self.state, self.log_bundle
        )

        signal.signal(signal.SIGTERM, self._on_shutdown)
        signal.signal(signal.SIGINT,  self._on_shutdown)
        signal.signal(signal.SIGHUP,  lambda s, f: log.info("SIGHUP received"))

        log.info("PDFWala Monitor v5 initialised on %s", HOSTNAME)

    def _on_shutdown(self, sig, _):
        log.info("Signal %s — shutting down", sig)
        self.running = False
        self._stop.set()

    def _find_docker_compose(self) -> Optional[List[str]]:
        for cmd in (["docker", "compose"], ["docker-compose"]):
            try:
                subprocess.run(cmd + ["version"],
                               capture_output=True, check=True, timeout=5)
                log.info("Docker Compose: %s", " ".join(cmd))
                return cmd
            except (subprocess.SubprocessError, OSError):
                pass
        log.warning("[DOCKER_ERR] Docker Compose not found — container checks disabled")
        return None

    def _heartbeat(self):
        try:
            HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat())
        except OSError:
            pass

    def _write_metrics(self, m: dict):
        try:
            self._metrics_history.append(m)
            METRICS_FILE.write_text(json.dumps(m, indent=2, default=str))
        except OSError:
            pass

    def _check_self_memory(self):
        """Warn if this process exceeds MEMORY_WARN_THRESHOLD_MB (Prompt 6)."""
        try:
            proc    = psutil.Process(os.getpid())
            mem_mb  = proc.memory_info().rss / (1024 * 1024)
            k       = "monitor_mem"
            threshold = int(MEMORY_WARN_THRESHOLD_MB)
            if mem_mb > threshold:
                if self.cd.should_fire(k):
                    log.warning(
                        "[MONITOR_ERR] Monitor process using %.1f MB "
                        "(threshold %d MB)", mem_mb, threshold)
                    self.tg.send(
                        f"⚠️ <b>Monitor self-memory high</b>: "
                        f"{mem_mb:.0f} MB on {HOSTNAME} "
                        f"(threshold {threshold} MB)",
                        priority=Priority.WARNING,
                    )
            else:
                self.cd.clear(k)
        except psutil.Error:
            pass

    # ── timed check wrapper ───────────────────────────────────

    def _timed(self, fn) -> List[Alert]:
        t0     = time.monotonic()
        result = fn()
        self._perf.record(fn.__name__, time.monotonic() - t0)
        return result

    # ══════════════════════════════════════════════════════════
    #  CHECK METHODS
    # ══════════════════════════════════════════════════════════

    def check_system(self) -> List[Alert]:
        alerts: List[Alert] = []
        cpu_thr = self.state.cpu_threshold
        ram_thr = self.state.ram_threshold
        try:
            # Non-blocking CPU read from background sampler (Prompt CPU opt)
            cpu = self._cpu_sampler.value
            k   = "cpu"
            if cpu > cpu_thr:
                if self.cd.should_fire(k):
                    alerts.append(Alert(k,
                        f"CPU at {cpu:.1f}% (threshold {cpu_thr}%)",
                        Sev.CRIT if cpu > 95 else Sev.WARN))
            else:
                self.cd.clear(k)

            ram = psutil.virtual_memory()
            k   = "ram"
            if ram.percent > ram_thr:
                if self.cd.should_fire(k):
                    alerts.append(Alert(k,
                        f"RAM {ram.percent:.1f}% "
                        f"({ram.used/1e9:.1f}/{ram.total/1e9:.1f} GB)",
                        Sev.CRIT if ram.percent > 95 else Sev.WARN))
            else:
                self.cd.clear(k)

            self._mem_samples.append(ram.percent)
            if len(self._mem_samples) == MEM_TREND_CYCLES + 1:
                growth = self._mem_samples[-1] - self._mem_samples[0]
                k = "mem_trend"
                if growth > MEM_TREND_DELTA * MEM_TREND_CYCLES:
                    if self.cd.should_fire(k):
                        alerts.append(Alert(k,
                            f"Memory growing +{growth:.1f}% over "
                            f"{MEM_TREND_CYCLES} cycles (possible leak)",
                            Sev.WARN))
                else:
                    self.cd.clear(k)

            swap = psutil.swap_memory()
            k = "swap"
            if swap.percent > SWAP_THRESHOLD:
                if self.cd.should_fire(k):
                    alerts.append(Alert(k,
                        f"Swap {swap.percent:.1f}% used", Sev.WARN))
            else:
                self.cd.clear(k)

            disk = psutil.disk_usage("/")
            k = "disk"
            if disk.percent > DISK_THRESHOLD:
                if self.cd.should_fire(k):
                    alerts.append(Alert(k,
                        f"Disk {disk.percent:.1f}% "
                        f"({disk.used/1e9:.1f}/{disk.total/1e9:.1f} GB)",
                        Sev.CRIT if disk.percent > 95 else Sev.WARN))
            else:
                self.cd.clear(k)

            load1, _, _ = os.getloadavg()
            cpus = psutil.cpu_count() or 1
            k = "load"
            if load1 > cpus * LOAD_MULTIPLIER:
                if self.cd.should_fire(k):
                    alerts.append(Alert(k,
                        f"Load avg {load1:.2f} on {cpus} CPUs "
                        f"(×{LOAD_MULTIPLIER} threshold)", Sev.WARN))
            else:
                self.cd.clear(k)

            try:
                import resource as _res
                soft, _ = _res.getrlimit(_res.RLIMIT_NOFILE)
                try:
                    fd_open = len(list(
                        Path(f"/proc/{os.getpid()}/fd").iterdir()))
                except OSError:
                    fd_open = psutil.Process(os.getpid()).num_fds()
                k = "fd"
                if soft and (fd_open / soft) > FD_THRESHOLD / 100:
                    if self.cd.should_fire(k):
                        alerts.append(Alert(k,
                            f"File descriptors {fd_open}/{soft} "
                            f"({fd_open*100//soft}%)", Sev.WARN))
                else:
                    self.cd.clear(k)
            except (ImportError, psutil.Error, OSError):
                pass

            zombies = [
                p for p in psutil.process_iter(["status"])
                if p.info.get("status") == psutil.STATUS_ZOMBIE
            ]
            k = "zombies"
            if zombies:
                if self.cd.should_fire(k):
                    alerts.append(Alert(k,
                        f"{len(zombies)} zombie process(es) detected",
                        Sev.WARN))
            else:
                self.cd.clear(k)

            self._check_disk_io(alerts)

        except psutil.AccessDenied as e:
            log.error("[PERM_ERR] System check access denied: %s", e)
            alerts.append(Alert("sys_perm",
                f"System check permission denied: {e}", Sev.WARN))
        except psutil.Error as e:
            log.error("[MONITOR_ERR] psutil error in system check: %s", e)
            alerts.append(Alert("sys_err",
                f"System check failed: {e}", Sev.WARN))
        return alerts

    def _check_disk_io(self, alerts: List[Alert]):
        try:
            now = time.monotonic()
            c   = psutil.disk_io_counters()
            if (self._prev_io is not None and c
                    and hasattr(c, "busy_time")):
                elapsed = now - self._prev_io_ts
                if elapsed > 0:
                    busy_ms  = c.busy_time - self._prev_io.busy_time
                    busy_pct = min((busy_ms / (elapsed * 1000)) * 100, 100)
                    k = "disk_io"
                    if busy_pct > DISK_IO_PCT:
                        if self.cd.should_fire(k):
                            alerts.append(Alert(k,
                                f"Disk I/O busy {busy_pct:.0f}% "
                                f"(threshold {DISK_IO_PCT}%)", Sev.WARN))
                    else:
                        self.cd.clear(k)
            if c:
                self._prev_io    = c
                self._prev_io_ts = now
        except psutil.Error:
            pass

    def check_ssl(self) -> List[Alert]:
        alerts: List[Alert] = []
        for host in ["npkpadala.com"]:
            k = f"ssl_{host}"
            try:
                ctx = ssl.create_default_context()
                with ctx.wrap_socket(
                    socket.create_connection((host, 443), timeout=10),
                    server_hostname=host,
                ) as s:
                    cert    = s.getpeercert()
                    exp_str = cert.get("notAfter", "")
                    exp_dt  = datetime.strptime(
                        exp_str, "%b %d %H:%M:%S %Y %Z"
                    ).replace(tzinfo=timezone.utc)
                    days = (exp_dt - datetime.now(timezone.utc)).days
                    if days < SSL_CRIT_DAYS:
                        if self.cd.should_fire(k):
                            alerts.append(Alert(k,
                                f"SSL {host} expires in {days} day(s)!",
                                Sev.CRIT))
                    elif days < SSL_WARN_DAYS:
                        if self.cd.should_fire(k):
                            alerts.append(Alert(k,
                                f"SSL {host} expires in {days} day(s)",
                                Sev.WARN))
                    else:
                        self.cd.clear(k)
            except ssl.SSLError as e:
                if self.cd.should_fire(k):
                    alerts.append(Alert(k,
                        f"SSL error {host}: {e}", Sev.CRIT))
            except socket.timeout:
                log.warning("[TIMEOUT_ERR] SSL check %s timed out", host)
            except OSError as e:
                log.warning("[NET_ERR] SSL check %s: %s", host, e)
        return alerts

    def check_containers(self) -> List[Alert]:
        alerts: List[Alert] = []
        if not self.docker_cmd:
            return []
        try:
            r = subprocess.run(["docker", "info"],
                               capture_output=True, timeout=10)
            k = "docker_daemon"
            if r.returncode != 0:
                if self.cd.should_fire(k):
                    alerts.append(Alert(k, "Docker daemon not running",
                                        Sev.CRIT))
                return alerts
            self.cd.clear(k)

            # Use container cache instead of fresh docker ps every cycle
            running = self._ct_cache.get()

            for name in EXPECTED_CONTAINERS:
                k = f"ct_{name}"
                if name not in running:
                    if self.cd.should_fire(k):
                        alerts.append(Alert(k,
                            f"Container {name} is MISSING",
                            Sev.CRIT, auto_action=f"restart:{name}"))
                elif "Up" not in running[name]:
                    if self.cd.should_fire(k):
                        alerts.append(Alert(k,
                            f"Container {name} unhealthy: {running[name]}",
                            Sev.CRIT, auto_action=f"restart:{name}"))
                else:
                    self.cd.clear(k)

        except subprocess.TimeoutExpired:
            log.error("[TIMEOUT_ERR] Docker check timed out")
            alerts.append(Alert("docker_timeout",
                                "Docker check timed out", Sev.WARN))
        except PermissionError as e:
            log.error("[PERM_ERR] Docker permission denied: %s", e)
            alerts.append(Alert("docker_perm",
                                f"Docker permission denied: {e}", Sev.CRIT))
        except OSError as e:
            log.error("[DOCKER_ERR] Container check OS error: %s", e)
            alerts.append(Alert("ct_err",
                                f"Container check failed: {e}", Sev.WARN))
        return alerts

    def check_redis(self) -> List[Alert]:
        alerts: List[Alert] = []
        k = "redis"
        try:
            r = subprocess.run(
                ["docker", "exec", REDIS_CONTAINER, "redis-cli", "ping"],
                capture_output=True, text=True, timeout=10,
            )
            if r.stdout.strip().upper() == "PONG":
                self.cd.clear(k)
            else:
                if self.cd.should_fire(k):
                    alerts.append(Alert(k,
                        f"Redis unexpected response: {r.stdout.strip()!r}",
                        Sev.WARN))
        except subprocess.TimeoutExpired:
            log.error("[TIMEOUT_ERR] Redis check timed out")
            if self.cd.should_fire(k):
                alerts.append(Alert(k, "Redis check timed out", Sev.CRIT))
        except OSError as e:
            log.error("[DOCKER_ERR] Redis check failed: %s", e)
            if self.cd.should_fire(k):
                alerts.append(Alert(k, f"Redis check failed: {e}", Sev.CRIT))
        return alerts

    def check_celery_queues(self) -> List[Alert]:
        alerts: List[Alert] = []
        for queue in CELERY_QUEUES:
            k = f"celery_q_{queue}"
            try:
                r = subprocess.run(
                    ["docker", "exec", REDIS_CONTAINER,
                     "redis-cli", "LLEN", queue],
                    capture_output=True, text=True, timeout=10,
                )
                depth_str = r.stdout.strip()
                if not depth_str.isdigit():
                    continue
                depth = int(depth_str)
                if depth >= CELERY_QUEUE_CRIT:
                    if self.cd.should_fire(k):
                        alerts.append(Alert(k,
                            f"Celery '{queue}' queue has {depth} jobs "
                            f"(critical >{CELERY_QUEUE_CRIT})", Sev.CRIT))
                elif depth >= CELERY_QUEUE_WARN:
                    if self.cd.should_fire(k):
                        alerts.append(Alert(k,
                            f"Celery '{queue}' queue has {depth} jobs "
                            f"(warn >{CELERY_QUEUE_WARN})", Sev.WARN))
                else:
                    self.cd.clear(k)
            except subprocess.TimeoutExpired:
                log.debug("[TIMEOUT_ERR] Celery queue check '%s' timed out",
                          queue)
            except OSError as e:
                log.debug("[DOCKER_ERR] Celery queue check '%s': %s", queue, e)
        return alerts

    def check_processes(self) -> List[Alert]:
        alerts: List[Alert] = []
        try:
            names = {p.info.get("name") or ""
                     for p in psutil.process_iter(["name"])}
            for proc, key in [("gunicorn", "proc_gunicorn"),
                              ("celery",   "proc_celery")]:
                found = any(proc in n for n in names)
                if not found:
                    if self.cd.should_fire(key):
                        alerts.append(Alert(key,
                            f"Process '{proc}' not found on host", Sev.WARN))
                else:
                    self.cd.clear(key)
        except psutil.Error as e:
            log.warning("[MONITOR_ERR] Process check failed: %s", e)
        return alerts

    def check_endpoints(self) -> List[Alert]:
        alerts: List[Alert] = []
        hdrs = {"User-Agent": "PDFWala-Monitor/5.0"}
        for name, url, expected, critical in ENDPOINTS:
            k    = f"ep_{name.replace(' ','_')}"
            k_sl = f"{k}_slow"
            try:
                t0   = time.monotonic()
                resp = requests.get(url, timeout=10, headers=hdrs,
                                    allow_redirects=True)
                ms   = (time.monotonic() - t0) * 1000
                if resp.status_code != expected:
                    if self.cd.should_fire(k):
                        alerts.append(Alert(k,
                            f"{name} returned HTTP {resp.status_code} "
                            f"(expected {expected})",
                            Sev.CRIT if critical else Sev.WARN))
                else:
                    self.cd.clear(k)
                if ms > 3000:
                    if self.cd.should_fire(k_sl):
                        alerts.append(Alert(k_sl,
                            f"{name} slow: {ms:.0f}ms", Sev.WARN))
                else:
                    self.cd.clear(k_sl)
            except requests.exceptions.Timeout:
                log.error("[TIMEOUT_ERR] Endpoint %s timed out", name)
                if self.cd.should_fire(k):
                    alerts.append(Alert(k, f"{name} timed out",
                        Sev.CRIT if critical else Sev.WARN))
            except requests.exceptions.ConnectionError as e:
                log.error("[NET_ERR] Endpoint %s connection error: %s",
                          name, e)
                if self.cd.should_fire(k):
                    alerts.append(Alert(k,
                        f"{name} connection failed: {str(e)[:80]}",
                        Sev.CRIT if critical else Sev.WARN))
            except requests.exceptions.RequestException as e:
                log.error("[NET_ERR] Endpoint %s error: %s", name, e)
                if self.cd.should_fire(k):
                    alerts.append(Alert(k,
                        f"{name} unreachable: {str(e)[:80]}",
                        Sev.CRIT if critical else Sev.WARN))
        return alerts

    def check_folders(self) -> List[Alert]:
        alerts: List[Alert] = []
        checks = [
            ("uploads", UPLOADS_DIR, UPLOAD_SIZE_GB),
            ("outputs", OUTPUTS_DIR, OUTPUT_SIZE_GB),
            ("temp",    TEMP_DIR,    TEMP_SIZE_GB),
        ]
        for name, path, max_gb in checks:
            if not path.exists():
                continue
            k = f"folder_{name}"
            try:
                r = subprocess.run(["du", "-s", str(path)],
                                   capture_output=True, text=True, timeout=30)
                if r.returncode == 0 and r.stdout.strip():
                    size_gb = int(r.stdout.split()[0]) / (1024 ** 2)
                    if size_gb > max_gb:
                        if self.cd.should_fire(k):
                            alerts.append(Alert(k,
                                f"Folder '{name}' is {size_gb:.1f} GB "
                                f"(limit {max_gb} GB)", Sev.WARN))
                    else:
                        self.cd.clear(k)
            except subprocess.TimeoutExpired:
                log.warning("[TIMEOUT_ERR] Folder check '%s' timed out", name)
            except OSError as e:
                log.warning("[MONITOR_ERR] Folder check '%s': %s", name, e)
        return alerts

    LOG_PATTERNS = [
        "ERROR", "Exception", "Traceback", "500", "502", "503",
        "Failed", "ModuleNotFoundError", "ImportError",
        "Killed", "OOM", "timeout", "Connection refused",
        "CRITICAL", "FATAL", "Segfault", "core dumped",
        "circuit breaker", "backpressure", "rate limit exceeded",
        "job ttl", "cleanup failed", "pdf corrupt",
    ]

    def check_logs(self) -> List[Alert]:
        alerts: List[Alert] = []
        if not self.docker_cmd:
            return []
        new_errors: List[str] = []
        for name in EXPECTED_CONTAINERS:
            label = name.replace("pdfwala-", "")
            try:
                r = subprocess.run(
                    # Prompt CPU opt: LOG_TAIL_LINES (default 50) vs old 100
                    ["docker", "logs", f"--tail={LOG_TAIL_LINES}",
                     "--no-color", name],
                    capture_output=True, text=True,
                    cwd=str(BASE_DIR), timeout=20,
                )
                logs = (r.stdout or "") + (r.stderr or "")
                # Python string ops instead of grep (Prompt CPU opt)
                for line in logs.splitlines():
                    ll = line.lower()
                    if any(p.lower() in ll for p in self.LOG_PATTERNS):
                        if self.log_store.is_new(f"{name}:{line}"):
                            new_errors.append(f"[{label}] {line[:180]}")
            except subprocess.TimeoutExpired:
                log.warning("[TIMEOUT_ERR] Log check %s timed out", name)
            except OSError as e:
                log.warning("[DOCKER_ERR] Log check '%s': %s", name, e)
        if new_errors:
            preview = "\n".join(new_errors[:5])
            suffix  = (f"\n…+{len(new_errors)-5} more"
                       if len(new_errors) > 5 else "")
            alerts.append(Alert("log_errors",
                f"{len(new_errors)} new error(s) in logs:\n"
                f"<pre>{preview}{suffix}</pre>",
                Sev.WARN))
        return alerts

    # ── auto-restart ──────────────────────────────────────────

    def _auto_restart(self, alerts: List[Alert]):
        if not self.docker_cmd:
            return
        for a in alerts:
            if not (a.auto_action
                    and a.auto_action.startswith("restart:")):
                continue
            name = a.auto_action.split(":", 1)[1]
            k    = f"ar_{name}"
            if not self.restart_cd.should_fire(k):
                log.info("Auto-restart cooldown active: %s", name)
                continue
            log.warning("Auto-restarting: %s", name)
            try:
                subprocess.run(
                    self.docker_cmd + ["restart", name],
                    cwd=str(BASE_DIR), capture_output=True,
                    timeout=60, check=True,
                )
                self.tg.send(
                    f"🔄 Auto-restarted <b>{name}</b> on {HOSTNAME}",
                    priority=Priority.WARNING,
                )
                log.info("Auto-restart OK: %s", name)
            except subprocess.TimeoutExpired:
                log.error("[TIMEOUT_ERR] Auto-restart timed out: %s", name)
                self.tg.send(
                    f"❌ Auto-restart TIMED OUT <b>{name}</b> on {HOSTNAME}",
                    priority=Priority.CRITICAL,
                )
            except subprocess.CalledProcessError as e:
                log.error("[DOCKER_ERR] Auto-restart FAILED %s: %s", name, e)
                self.tg.send(
                    f"❌ Auto-restart FAILED <b>{name}</b> on {HOSTNAME}",
                    priority=Priority.CRITICAL,
                )

    # ── summary ───────────────────────────────────────────────

    def summary(self) -> str:
        try:
            cpu  = self._cpu_sampler.value
            ram  = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            swap = psutil.swap_memory()
            l1, l5, _ = os.getloadavg()
            up_h  = int((time.time() - psutil.boot_time()) // 3600)
            net   = psutil.net_io_counters()
            lines = [
                "<b>📊 PDFWala 6h Status Report</b>",
                f"🖥️  <b>{HOSTNAME}</b>  •  "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M')}",
                "",
                "<b>⚙️  System</b>",
                f"  CPU : {cpu:.1f}%   Load: {l1:.2f}/{l5:.2f}",
                f"  RAM : {ram.percent:.1f}%  "
                f"({ram.used/1e9:.1f}/{ram.total/1e9:.1f} GB)",
                f"  Swap: {swap.percent:.1f}%",
                f"  Disk: {disk.percent:.1f}%  (free {disk.free/1e9:.1f} GB)",
                f"  Up  : {up_h//24}d {up_h%24}h",
                f"  Net : ↓{net.bytes_recv/1e6:.0f}MB  "
                f"↑{net.bytes_sent/1e6:.0f}MB",
                "",
                "<b>📁  Storage</b>",
            ]
            for name, path in [
                ("uploads", UPLOADS_DIR),
                ("outputs", OUTPUTS_DIR),
                ("temp",    TEMP_DIR),
            ]:
                if path.exists():
                    try:
                        r = subprocess.run(
                            ["du", "-sh", str(path)],
                            capture_output=True, text=True, timeout=10)
                        size = r.stdout.split()[0] if r.stdout else "N/A"
                    except (subprocess.TimeoutExpired, OSError):
                        size = "err"
                    lines.append(f"  {name}: {size}")

            lines += ["", "<b>📬  Celery Queues</b>"]
            for queue in CELERY_QUEUES:
                try:
                    r = subprocess.run(
                        ["docker", "exec", REDIS_CONTAINER,
                         "redis-cli", "LLEN", queue],
                        capture_output=True, text=True, timeout=5,
                    )
                    depth = (r.stdout.strip()
                             if r.stdout.strip().isdigit() else "?")
                    lines.append(f"  {queue}: {depth} jobs")
                except (subprocess.TimeoutExpired, OSError):
                    lines.append(f"  {queue}: ?")

            lines += [
                "",
                f"<b>🔔  Active cooldown keys:</b> {self.cd.active_count()}",
                f"<b>📦  Log archives:</b> kept {LOG_ARCHIVE_DAYS} days  "
                f"(sent daily at "
                f"{LOG_BUNDLE_HOUR:02d}:{LOG_BUNDLE_MINUTE:02d})",
                f"<b>🤖  Bot commands:</b> active  "
                f"(poll every {CMD_POLL_INTERVAL}s)",
                f"<b>📊  TG rate-limit hits:</b> {self.tg.rate_limit_hits}",
            ]

            # Add perf report if enabled
            perf_report = self._perf.report()
            if perf_report:
                lines += ["", perf_report]

            return "\n".join(lines)
        except psutil.Error as e:
            log.exception("[MONITOR_ERR] Summary failed")
            return f"❌ Summary failed: {e}"

    # ── main loop ─────────────────────────────────────────────

    def run(self):
        log.info("Running Telegram credential test…")
        if not self.tg.test():
            log.critical("[NET_ERR] Telegram test FAILED — check TOKEN / CHAT_ID")
            sys.exit(1)

        _check_ghost_containers(self.tg)
        self.cmd_handler.start()

        self.tg.send(
            f"🟢 <b>PDFWala Monitor v5 started</b>\n"
            f"🖥️  {HOSTNAME}\n"
            f"⚙️  CPU&gt;{self.state.cpu_threshold}%  "
            f"RAM&gt;{self.state.ram_threshold}%  "
            f"Disk&gt;{DISK_THRESHOLD}%\n"
            f"📦 Daily logs → Telegram at "
            f"{LOG_BUNDLE_HOUR:02d}:{LOG_BUNDLE_MINUTE:02d}  "
            f"(kept {LOG_ARCHIVE_DAYS} days)\n"
            f"🤖 Bot commands: active (type /help to see all)\n"
            f"🔔 Cooldown: {ALERT_COOLDOWN}s  "
            f"Interval: {CHECK_INTERVAL}s",
            priority=Priority.INFO,
        )

        summary_acc   = 0
        cleanup_cycle = 0

        while self.running:
            t0 = time.monotonic()

            muted = self.state.is_muted()

            all_alerts: List[Alert] = []
            for fn in (
                self.check_system,
                self.check_ssl,
                self.check_containers,
                self.check_redis,
                self.check_celery_queues,
                self.check_processes,
                self.check_endpoints,
                self.check_folders,
                self.check_logs,
            ):
                try:
                    all_alerts.extend(self._timed(fn))
                except Exception as e:
                    log.exception("[MONITOR_ERR] Check %s failed: %s",
                                  fn.__name__, e)

            self._auto_restart(all_alerts)
            self._check_self_memory()

            if not muted:
                crits = [a for a in all_alerts if a.severity == Sev.CRIT]
                warns = [a for a in all_alerts if a.severity == Sev.WARN]
                if crits:
                    body = "\n".join(
                        f"  • {a.message}" for a in crits[:12])
                    self.tg.send(
                        f"🚨 <b>CRITICAL — {HOSTNAME}</b>\n\n{body}",
                        priority=Priority.CRITICAL,
                    )
                if warns:
                    body = "\n".join(
                        f"  • {a.message}" for a in warns[:12])
                    self.tg.send(
                        f"⚠️ <b>WARNING — {HOSTNAME}</b>\n\n{body}",
                        priority=Priority.WARNING,
                    )
                if all_alerts:
                    log.warning("Cycle: %d critical, %d warning",
                                len(crits), len(warns))
            else:
                if all_alerts:
                    log.info("Muted: suppressed %d alert(s)",
                             len(all_alerts))

            # Metrics
            try:
                ram  = psutil.virtual_memory()
                disk = psutil.disk_usage("/")
                self._write_metrics({
                    "ts":           datetime.now(timezone.utc).isoformat(),
                    "cpu_pct":      self._cpu_sampler.value,
                    "ram_pct":      ram.percent,
                    "ram_used_gb":  round(ram.used / 1e9, 2),
                    "disk_pct":     disk.percent,
                    "disk_free_gb": round(disk.free / 1e9, 2),
                    "swap_pct":     psutil.swap_memory().percent,
                    "load1":        round(os.getloadavg()[0], 2),
                    "alerts_crit":  len([a for a in all_alerts
                                         if a.severity == Sev.CRIT]),
                    "alerts_warn":  len([a for a in all_alerts
                                         if a.severity == Sev.WARN]),
                    "muted":        muted,
                    "tg_rate_hits": self.tg.rate_limit_hits,
                })
            except (psutil.Error, OSError):
                pass

            self._heartbeat()

            summary_acc += CHECK_INTERVAL
            if summary_acc >= SUMMARY_INTERVAL:
                self.tg.send(self.summary(), priority=Priority.INFO)
                summary_acc = 0
                log.info("6-hour summary sent")

            self.log_bundle.tick()

            # Periodic LogHashStore cleanup (Prompt 6) every ~1 hour
            cleanup_cycle += 1
            if cleanup_cycle >= 60:
                self.log_store.cleanup()
                cleanup_cycle = 0

            elapsed   = time.monotonic() - t0
            # Jitter ±10% to avoid thundering herd (Prompt CPU opt)
            jitter    = random.uniform(-0.1, 0.1) * CHECK_INTERVAL
            sleep_for = max(0, CHECK_INTERVAL - elapsed + jitter)
            self._stop.wait(timeout=sleep_for)

        log.info("Monitor stopped")
        self.tg.send(f"🔴 <b>PDFWala Monitor stopped</b> on {HOSTNAME}",
                     priority=Priority.INFO)


# ╔══════════════════════════════════════════════════════════════
# ║  HELPERS
# ╚══════════════════════════════════════════════════════════════

def _validate_config():
    """Legacy stub — validation now happens in Config.__init__."""
    pass


def _check_ghost_containers(tg: Telegram):
    try:
        r = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.RunningFor}}"],
            capture_output=True, text=True, timeout=10,
        )
        ghosts = []
        for line in r.stdout.strip().splitlines():
            parts = line.split("\t", 1)
            if len(parts) != 2:
                continue
            name, running_for = parts
            if "pdfwala" in name and name not in EXPECTED_CONTAINERS:
                ghosts.append(f"  {name}  (running {running_for})")
        if ghosts:
            ghost_list = "\n".join(ghosts)
            msg = (
                f"👻 <b>Ghost container(s) detected on {HOSTNAME}</b>\n\n"
                f"<pre>{ghost_list}</pre>\n\n"
                f"Clean up with:\n"
                f"<code>docker stop &lt;name&gt; "
                f"&amp;&amp; docker rm &lt;name&gt;</code>"
            )
            log.warning("Ghost containers found: %s", ghosts)
            tg.send(msg, priority=Priority.WARNING)
    except subprocess.TimeoutExpired:
        log.warning("[TIMEOUT_ERR] Ghost container check timed out")
    except OSError as e:
        log.warning("[DOCKER_ERR] Ghost container check failed: %s", e)


# ╔══════════════════════════════════════════════════════════════
# ║  ENTRY POINT
# ╚══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        Monitor().run()
    except KeyboardInterrupt:
        print("\nInterrupted")
    except SystemExit:
        raise
    except Exception as e:
        log.critical("[MONITOR_ERR] Fatal: %s", e, exc_info=True)
        sys.exit(1)
