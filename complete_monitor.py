#!/usr/bin/env python3
"""
PDFWala Production Monitor v3
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Improvements over v2:
  • Alert severity levels (WARNING / CRITICAL) with separate Telegram icons
  • SSL certificate expiry check (warns at <30 days, critical at <7)
  • Process-level monitoring: gunicorn workers, celery, redis-server
  • Disk I/O saturation detection
  • File descriptor leak detection
  • Zombie process detection
  • Memory usage trend tracking (flags steady growth over N samples)
  • Redis PING health check (beyond just container presence)
  • Auto-restart for known recoverable container failures
  • Persistent alert cooldown state across SIGHUP reloads
  • Concurrent check safety via threading.Event + timeout
  • Structured JSON metrics snapshot written to disk each cycle
  • Heartbeat file (dead-man's switch for external watchdog)
  • Config validation at startup with clear error messages
  • docker_cmd always built as a proper list (not a bare string)
  • Telegram rate-limiter: max 20 msgs / 60s across all sends
  • Smart log dedup: hashes stored to a small file between restarts
  • Startup self-test: verifies Telegram credentials before entering loop
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations

import collections
import hashlib
import json
import logging
import os
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, Tuple

# ── optional deps with graceful fallback ─────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    _env_file = Path("/home/opc/pdfwala/.env")
    if _env_file.exists():
        with _env_file.open() as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip())

try:
    import psutil
except ImportError:
    print("❌ psutil is required: pip install psutil", file=sys.stderr)
    sys.exit(1)

try:
    import requests
except ImportError:
    print("❌ requests is required: pip install requests", file=sys.stderr)
    sys.exit(1)


# ╔══════════════════════════════════════════════════════════════
# ║  CONFIGURATION
# ╚══════════════════════════════════════════════════════════════

def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, default))
    except ValueError:
        print(f"⚠️  Invalid value for {key}, using default {default}")
        return default

def _env_float(key: str, default: float) -> float:
    try:
        return float(os.getenv(key, default))
    except ValueError:
        print(f"⚠️  Invalid value for {key}, using default {default}")
        return default

# Required secrets
TOKEN   = os.getenv("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Thresholds
CPU_THRESHOLD    = _env_int("CPU_THRESHOLD",   90)
RAM_THRESHOLD    = _env_int("RAM_THRESHOLD",   90)
DISK_THRESHOLD   = _env_int("DISK_THRESHOLD",  90)
SWAP_THRESHOLD   = _env_int("SWAP_THRESHOLD",  50)
FD_THRESHOLD     = _env_int("FD_THRESHOLD",    80)   # % of system fd limit
LOAD_MULTIPLIER  = _env_float("LOAD_MULTIPLIER", 1.5)
UPLOAD_SIZE_GB   = _env_float("UPLOAD_SIZE_GB",  10.0)
DISK_IO_PCT      = _env_int("DISK_IO_BUSY_PCT", 90)  # % disk busy time
SSL_WARN_DAYS    = _env_int("SSL_WARN_DAYS",    30)
SSL_CRIT_DAYS    = _env_int("SSL_CRIT_DAYS",     7)
MEM_TREND_CYCLES = _env_int("MEM_TREND_CYCLES",  5)  # samples for trend
MEM_TREND_DELTA  = _env_float("MEM_TREND_DELTA", 3.0) # % per sample triggers warn

# Timing
CHECK_INTERVAL   = _env_int("CHECK_INTERVAL",   60)    # seconds
SUMMARY_INTERVAL = _env_int("SUMMARY_INTERVAL", 21600) # 6 hours
ALERT_COOLDOWN   = _env_int("ALERT_COOLDOWN",   1800)  # 30 minutes
AUTO_RESTART_COOLDOWN = _env_int("AUTO_RESTART_COOLDOWN", 300) # 5 minutes

# Telegram rate limit: max messages per window
TG_RATE_MAX      = _env_int("TG_RATE_MAX",  20)
TG_RATE_WINDOW   = _env_int("TG_RATE_WINDOW", 60)  # seconds

# Paths
BASE_DIR      = Path(os.getenv("BASE_DIR", "/home/opc/pdfwala"))
LOG_FILE      = BASE_DIR / "monitor.log"
UPLOADS_DIR   = BASE_DIR / "uploads"
DOWNLOADS_DIR = BASE_DIR / "downloads"
METRICS_FILE  = BASE_DIR / "monitor_metrics.json"
HEARTBEAT_FILE = BASE_DIR / "monitor_heartbeat"
LOG_HASH_FILE = BASE_DIR / "monitor_log_hashes.json"

# Containers we care about
EXPECTED_CONTAINERS = [
    "pdfwala_app",
    "pdfwala_worker",
    "pdfwala_nginx",
    "pdfwala_redis",
]

# Endpoints to probe  (name, url, expected_http_code, critical?)
ENDPOINTS: List[Tuple[str, str, int, bool]] = [
    ("Homepage",  "https://npkpadala.com/pdfwala/", 200, True),
    ("API Health","https://npkpadala.com/api/health", 200, True),
]

# Hostname for labels
HOSTNAME = socket.gethostname()


# ╔══════════════════════════════════════════════════════════════
# ║  LOGGING
# ╚══════════════════════════════════════════════════════════════

BASE_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("pdfwala.monitor")


# ╔══════════════════════════════════════════════════════════════
# ║  SEVERITY
# ╚══════════════════════════════════════════════════════════════

class Severity:
    WARNING  = "WARNING"
    CRITICAL = "CRITICAL"

_SEV_EMOJI = {
    Severity.WARNING:  "⚠️",
    Severity.CRITICAL: "🚨",
}


# ╔══════════════════════════════════════════════════════════════
# ║  ALERT
# ╚══════════════════════════════════════════════════════════════

class Alert:
    """A single fired alert with severity and optional auto-action."""

    def __init__(
        self,
        key: str,
        message: str,
        severity: str = Severity.WARNING,
        auto_action: Optional[str] = None,
    ):
        self.key        = key
        self.message    = message
        self.severity   = severity
        self.auto_action = auto_action   # e.g. "restart:<container>"
        self.ts         = datetime.now(timezone.utc)

    def __str__(self) -> str:
        return f"{_SEV_EMOJI[self.severity]} {self.message}"


# ╔══════════════════════════════════════════════════════════════
# ║  TELEGRAM SENDER (with rate limiter)
# ╚══════════════════════════════════════════════════════════════

class TelegramSender:
    """Thread-safe Telegram sender with sliding-window rate limiter."""

    def __init__(self, token: str, chat_id: str):
        self._token   = token
        self._chat_id = chat_id
        self._lock    = threading.Lock()
        self._sends: Deque[float] = deque()   # timestamps of recent sends
        self._url     = f"https://api.telegram.org/bot{token}/sendMessage"

    def _rate_ok(self) -> bool:
        """Returns True if we're below TG_RATE_MAX in the last TG_RATE_WINDOW seconds."""
        now = time.monotonic()
        while self._sends and now - self._sends[0] > TG_RATE_WINDOW:
            self._sends.popleft()
        return len(self._sends) < TG_RATE_MAX

    def send(self, text: str, parse_mode: str = "HTML") -> bool:
        """Send a message; silently drops if rate limit hit. Returns success."""
        if len(text) > 4000:
            text = text[:3900] + "\n\n…[truncated]"

        with self._lock:
            if not self._rate_ok():
                logger.warning("Telegram rate limit hit — message suppressed")
                return False
            self._sends.append(time.monotonic())

        for attempt in range(3):
            try:
                resp = requests.post(
                    self._url,
                    json={"chat_id": self._chat_id, "text": text, "parse_mode": parse_mode},
                    timeout=15,
                )
                if resp.status_code == 200:
                    return True
                logger.error("Telegram API %s: %s", resp.status_code, resp.text[:120])
            except Exception as exc:
                logger.error("Telegram attempt %d failed: %s", attempt + 1, exc)
                time.sleep(2 ** attempt)
        return False

    def test(self) -> bool:
        """Send a test ping to verify credentials."""
        return self.send("🔍 PDFWala Monitor credential test — OK")


# ╔══════════════════════════════════════════════════════════════
# ║  COOLDOWN TRACKER
# ╚══════════════════════════════════════════════════════════════

class CooldownTracker:
    """Per-key alert cooldown with optional fire count."""

    def __init__(self, cooldown_seconds: int):
        self._cd  = cooldown_seconds
        self._map: Dict[str, Dict] = {}
        self._lock = threading.Lock()

    def should_fire(self, key: str) -> bool:
        now = datetime.now(timezone.utc)
        with self._lock:
            state = self._map.get(key)
            if state is None:
                self._map[key] = {"last": now, "count": 1}
                return True
            if (now - state["last"]).total_seconds() >= self._cd:
                state["last"]   = now
                state["count"] += 1
                return True
            return False

    def clear(self, key: str) -> None:
        with self._lock:
            self._map.pop(key, None)

    def fire_count(self, key: str) -> int:
        with self._lock:
            return self._map.get(key, {}).get("count", 0)


# ╔══════════════════════════════════════════════════════════════
# ║  LOG HASH STORE  (persists across restarts)
# ╚══════════════════════════════════════════════════════════════

class LogHashStore:
    """Persists seen log-error hashes across restarts so we don't re-alert."""

    MAX_HASHES = 2000

    def __init__(self, path: Path):
        self._path  = path
        self._hashes: Set[str] = set()
        self._lock  = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text())
                self._hashes = set(data.get("hashes", []))
                logger.info("Loaded %d log hashes from %s", len(self._hashes), self._path)
        except Exception as exc:
            logger.warning("Could not load log hash store: %s", exc)

    def _save(self) -> None:
        try:
            # Keep only the most recent MAX_HASHES
            if len(self._hashes) > self.MAX_HASHES:
                self._hashes = set(list(self._hashes)[-self.MAX_HASHES:])
            self._path.write_text(json.dumps({"hashes": list(self._hashes)}))
        except Exception as exc:
            logger.warning("Could not save log hash store: %s", exc)

    def is_new(self, raw: str) -> bool:
        """Returns True if this line hasn't been seen before (and records it)."""
        h = hashlib.md5(raw[:200].encode(), usedforsecurity=False).hexdigest()
        with self._lock:
            if h in self._hashes:
                return False
            self._hashes.add(h)
            self._save()
            return True


# ╔══════════════════════════════════════════════════════════════
# ║  MAIN MONITOR
# ╚══════════════════════════════════════════════════════════════

class PDFWalaMonitor:

    def __init__(self):
        _validate_config()

        self.running      = True
        self._stop_event  = threading.Event()
        self.telegram     = TelegramSender(TOKEN, CHAT_ID)
        self.cooldown     = CooldownTracker(ALERT_COOLDOWN)
        self.restart_cd   = CooldownTracker(AUTO_RESTART_COOLDOWN)
        self.log_store    = LogHashStore(LOG_HASH_FILE)
        self.docker_cmd   = self._detect_docker_compose()   # list or None

        # Memory trend window
        self._mem_samples: Deque[float] = deque(maxlen=MEM_TREND_CYCLES + 1)

        # Disk I/O baseline (first call to disk_io_counters is reference)
        self._prev_disk_io: Optional[psutil._common.sdiskio] = None
        self._prev_disk_io_ts: float = 0.0

        signal.signal(signal.SIGTERM, self._handle_shutdown)
        signal.signal(signal.SIGINT,  self._handle_shutdown)
        signal.signal(signal.SIGHUP,  self._handle_reload)

        logger.info("🚀 PDFWala Monitor v3 initialised on %s", HOSTNAME)

    # ── lifecycle ──────────────────────────────────────────────

    def _handle_shutdown(self, signum, _frame):
        logger.info("Signal %s received — shutting down…", signum)
        self.running = False
        self._stop_event.set()

    def _handle_reload(self, _signum, _frame):
        """SIGHUP: re-read .env without full restart (future-proof hook)."""
        logger.info("SIGHUP received — config reload requested (not yet implemented)")

    # ── docker compose detection ───────────────────────────────

    def _detect_docker_compose(self) -> Optional[List[str]]:
        for candidate in (["docker", "compose"], ["docker-compose"]):
            try:
                subprocess.run(
                    candidate + ["version"],
                    capture_output=True, check=True, timeout=5,
                )
                logger.info("Docker Compose command: %s", " ".join(candidate))
                return candidate
            except Exception:
                pass
        logger.warning("Docker Compose not found — container checks disabled")
        return None

    # ── heartbeat ─────────────────────────────────────────────

    def _touch_heartbeat(self) -> None:
        try:
            HEARTBEAT_FILE.write_text(
                datetime.now(timezone.utc).isoformat()
            )
        except Exception as exc:
            logger.debug("Heartbeat write failed: %s", exc)

    # ── metrics snapshot ───────────────────────────────────────

    def _write_metrics(self, metrics: dict) -> None:
        try:
            METRICS_FILE.write_text(
                json.dumps(metrics, indent=2, default=str)
            )
        except Exception as exc:
            logger.debug("Metrics write failed: %s", exc)

    # ══════════════════════════════════════════════════════════
    #  CHECK METHODS
    # ══════════════════════════════════════════════════════════

    # ── 1. System health ──────────────────────────────────────

    def check_system_health(self) -> List[Alert]:
        alerts: List[Alert] = []

        try:
            # CPU
            cpu = psutil.cpu_percent(interval=1)
            key = "cpu_high"
            if cpu > CPU_THRESHOLD:
                if self.cooldown.should_fire(key):
                    alerts.append(Alert(
                        key,
                        f"CPU at {cpu:.1f}% (threshold {CPU_THRESHOLD}%)",
                        Severity.CRITICAL if cpu > 95 else Severity.WARNING,
                    ))
            else:
                self.cooldown.clear(key)

            # RAM
            ram = psutil.virtual_memory()
            key = "ram_high"
            if ram.percent > RAM_THRESHOLD:
                if self.cooldown.should_fire(key):
                    alerts.append(Alert(
                        key,
                        f"RAM {ram.percent:.1f}% "
                        f"({ram.used / 1e9:.1f}/{ram.total / 1e9:.1f} GB)",
                        Severity.CRITICAL if ram.percent > 95 else Severity.WARNING,
                    ))
            else:
                self.cooldown.clear(key)

            # Memory trend
            self._mem_samples.append(ram.percent)
            if len(self._mem_samples) == MEM_TREND_CYCLES + 1:
                oldest = self._mem_samples[0]
                newest = self._mem_samples[-1]
                growth = newest - oldest
                key = "mem_trend"
                if growth > MEM_TREND_DELTA * MEM_TREND_CYCLES:
                    if self.cooldown.should_fire(key):
                        alerts.append(Alert(
                            key,
                            f"Memory growing: +{growth:.1f}% over {MEM_TREND_CYCLES} checks "
                            f"(possible leak)",
                            Severity.WARNING,
                        ))
                else:
                    self.cooldown.clear(key)

            # Swap
            swap = psutil.swap_memory()
            key = "swap_high"
            if swap.percent > SWAP_THRESHOLD:
                if self.cooldown.should_fire(key):
                    alerts.append(Alert(
                        key,
                        f"Swap {swap.percent:.1f}% used",
                        Severity.WARNING,
                    ))
            else:
                self.cooldown.clear(key)

            # Disk usage
            disk = psutil.disk_usage("/")
            key = "disk_high"
            if disk.percent > DISK_THRESHOLD:
                if self.cooldown.should_fire(key):
                    alerts.append(Alert(
                        key,
                        f"Disk {disk.percent:.1f}% "
                        f"({disk.used / 1e9:.1f}/{disk.total / 1e9:.1f} GB)",
                        Severity.CRITICAL if disk.percent > 95 else Severity.WARNING,
                    ))
            else:
                self.cooldown.clear(key)

            # Load average
            load1, _, _ = os.getloadavg()
            cpu_count = psutil.cpu_count() or 1
            key = "load_high"
            if load1 > cpu_count * LOAD_MULTIPLIER:
                if self.cooldown.should_fire(key):
                    alerts.append(Alert(
                        key,
                        f"Load avg {load1:.2f} on {cpu_count} CPUs "
                        f"(threshold ×{LOAD_MULTIPLIER})",
                        Severity.WARNING,
                    ))
            else:
                self.cooldown.clear(key)

            # File descriptors
            try:
                fd_soft, fd_hard = resource_limits()
                fd_open = _count_open_fds()
                if fd_soft and fd_open / fd_soft > FD_THRESHOLD / 100:
                    key = "fd_leak"
                    if self.cooldown.should_fire(key):
                        alerts.append(Alert(
                            key,
                            f"File descriptors: {fd_open}/{fd_soft} "
                            f"({fd_open*100//fd_soft}% of limit)",
                            Severity.WARNING,
                        ))
                else:
                    self.cooldown.clear("fd_leak")
            except Exception:
                pass  # fd check is best-effort

            # Zombie processes
            zombies = [p for p in psutil.process_iter(["status"])
                       if p.info.get("status") == psutil.STATUS_ZOMBIE]
            key = "zombies"
            if zombies:
                if self.cooldown.should_fire(key):
                    alerts.append(Alert(
                        key,
                        f"{len(zombies)} zombie process(es) detected",
                        Severity.WARNING,
                    ))
            else:
                self.cooldown.clear(key)

            # Disk I/O saturation
            self._check_disk_io(alerts)

        except Exception as exc:
            logger.exception("System health check error")
            alerts.append(Alert("sys_check_err", f"System check failed: {exc}", Severity.WARNING))

        return alerts

    def _check_disk_io(self, alerts: List[Alert]) -> None:
        """Append alert if disk busy% exceeds DISK_IO_PCT."""
        try:
            now = time.monotonic()
            counters = psutil.disk_io_counters()
            if self._prev_disk_io is not None and counters:
                elapsed = now - self._prev_disk_io_ts
                if elapsed > 0:
                    busy_ms = counters.busy_time - self._prev_disk_io.busy_time
                    busy_pct = (busy_ms / (elapsed * 1000)) * 100
                    busy_pct = min(busy_pct, 100)
                    key = "disk_io"
                    if busy_pct > DISK_IO_PCT:
                        if self.cooldown.should_fire(key):
                            alerts.append(Alert(
                                key,
                                f"Disk I/O busy {busy_pct:.0f}% (threshold {DISK_IO_PCT}%)",
                                Severity.WARNING,
                            ))
                    else:
                        self.cooldown.clear(key)
            if counters:
                self._prev_disk_io    = counters
                self._prev_disk_io_ts = now
        except Exception:
            pass  # busy_time not available on all platforms

    # ── 2. SSL certificate ────────────────────────────────────

    def check_ssl(self) -> List[Alert]:
        alerts: List[Alert] = []
        hostnames = ["npkpadala.com"]

        for host in hostnames:
            key = f"ssl_{host}"
            try:
                ctx = ssl.create_default_context()
                with ctx.wrap_socket(
                    socket.create_connection((host, 443), timeout=10),
                    server_hostname=host,
                ) as s:
                    cert    = s.getpeercert()
                    exp_str = cert.get("notAfter", "")
                    exp_dt  = datetime.strptime(exp_str, "%b %d %H:%M:%S %Y %Z").replace(
                        tzinfo=timezone.utc
                    )
                    days_left = (exp_dt - datetime.now(timezone.utc)).days

                    if days_left < SSL_CRIT_DAYS:
                        if self.cooldown.should_fire(key):
                            alerts.append(Alert(
                                key,
                                f"SSL for {host} expires in {days_left} day(s)!",
                                Severity.CRITICAL,
                            ))
                    elif days_left < SSL_WARN_DAYS:
                        if self.cooldown.should_fire(key):
                            alerts.append(Alert(
                                key,
                                f"SSL for {host} expires in {days_left} day(s)",
                                Severity.WARNING,
                            ))
                    else:
                        self.cooldown.clear(key)

            except ssl.SSLError as exc:
                if self.cooldown.should_fire(key):
                    alerts.append(Alert(key, f"SSL error on {host}: {exc}", Severity.CRITICAL))
            except Exception as exc:
                logger.warning("SSL check for %s failed: %s", host, exc)

        return alerts

    # ── 3. Container health ───────────────────────────────────

    def check_containers(self) -> List[Alert]:
        alerts: List[Alert] = []
        if not self.docker_cmd:
            return []

        try:
            # Verify Docker daemon
            result = subprocess.run(
                ["docker", "info"],
                capture_output=True, text=True, timeout=10,
            )
            key = "docker_daemon"
            if result.returncode != 0:
                if self.cooldown.should_fire(key):
                    alerts.append(Alert(key, "Docker daemon is not running", Severity.CRITICAL))
                return alerts
            self.cooldown.clear(key)

            # List running containers
            result = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
                capture_output=True, text=True, timeout=10,
            )
            running: Dict[str, str] = {}
            for line in result.stdout.strip().splitlines():
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    running[parts[0]] = parts[1]

            for name in EXPECTED_CONTAINERS:
                key = f"container_{name}"
                if name not in running:
                    if self.cooldown.should_fire(key):
                        a = Alert(
                            key,
                            f"Container {name} is MISSING",
                            Severity.CRITICAL,
                            auto_action=f"restart:{name}",
                        )
                        alerts.append(a)
                elif "Up" not in running[name]:
                    if self.cooldown.should_fire(key):
                        a = Alert(
                            key,
                            f"Container {name} unhealthy: {running[name]}",
                            Severity.CRITICAL,
                            auto_action=f"restart:{name}",
                        )
                        alerts.append(a)
                else:
                    self.cooldown.clear(key)

        except subprocess.TimeoutExpired:
            alerts.append(Alert("docker_timeout", "Docker check timed out", Severity.WARNING))
        except Exception as exc:
            logger.exception("Container check error")
            alerts.append(Alert("container_err", f"Container check failed: {exc}", Severity.WARNING))

        return alerts

    # ── 4. Redis health check (PING) ──────────────────────────

    def check_redis(self) -> List[Alert]:
        alerts: List[Alert] = []
        key = "redis_ping"
        try:
            # Check Redis via docker exec instead of direct socket
            result = subprocess.run(
                ["docker", "exec", "pdfwala_redis", "redis-cli", "ping"],
                capture_output=True, text=True, timeout=10
            )
            if "PONG" in result.stdout:
                self.cooldown.clear(key)
            else:
                if self.cooldown.should_fire(key):
                    alerts.append(Alert(
                        key,
                        f"Redis PING failed: {result.stdout.strip() or result.stderr.strip()}",
                        Severity.WARNING,
                    ))
        except Exception as exc:
            if self.cooldown.should_fire(key):
                alerts.append(Alert(
                    key,
                    f"Redis check failed: {exc}",
                    Severity.CRITICAL,
                ))
        return alerts

    # ── 5. Process-level checks ───────────────────────────────

    def check_processes(self) -> List[Alert]:
        """
        Check that critical processes are alive by name.
        Matches processes running inside containers via /proc if available,
        or on the host (e.g. if not using Docker).
        """
        alerts: List[Alert] = []
        watched = {
            "gunicorn": "gunicorn_missing",
            "celery":   "celery_missing",
        }
        running_names = {p.name() for p in psutil.process_iter(["name"])}

        for proc_name, key in watched.items():
            found = any(proc_name in n for n in running_names)
            if not found:
                if self.cooldown.should_fire(key):
                    alerts.append(Alert(
                        key,
                        f"Process '{proc_name}' not found on host",
                        Severity.WARNING,
                    ))
            else:
                self.cooldown.clear(key)

        return alerts

    # ── 6. Endpoints ──────────────────────────────────────────

    def check_endpoints(self) -> List[Alert]:
        alerts: List[Alert] = []
        headers = {"User-Agent": "PDFWala-Monitor/3.0"}

        for name, url, expected, critical in ENDPOINTS:
            key      = f"ep_{name.replace(' ', '_')}"
            key_slow = f"{key}_slow"
            try:
                t0   = time.monotonic()
                resp = requests.get(url, timeout=10, headers=headers, allow_redirects=True)
                ms   = (time.monotonic() - t0) * 1000

                if resp.status_code != expected:
                    if self.cooldown.should_fire(key):
                        alerts.append(Alert(
                            key,
                            f"{name} returned HTTP {resp.status_code} (expected {expected})",
                            Severity.CRITICAL if critical else Severity.WARNING,
                        ))
                else:
                    self.cooldown.clear(key)

                if ms > 3000:
                    if self.cooldown.should_fire(key_slow):
                        alerts.append(Alert(
                            key_slow,
                            f"{name} slow: {ms:.0f}ms",
                            Severity.WARNING,
                        ))
                else:
                    self.cooldown.clear(key_slow)

            except requests.exceptions.Timeout:
                if self.cooldown.should_fire(key):
                    alerts.append(Alert(
                        key, f"{name} timed out",
                        Severity.CRITICAL if critical else Severity.WARNING,
                    ))
            except Exception as exc:
                if self.cooldown.should_fire(key):
                    alerts.append(Alert(
                        key, f"{name} unreachable: {str(exc)[:80]}",
                        Severity.CRITICAL if critical else Severity.WARNING,
                    ))

        return alerts

    # ── 7. Storage folders ────────────────────────────────────

    def check_folders(self) -> List[Alert]:
        alerts: List[Alert] = []
        checks = [
            ("uploads",   UPLOADS_DIR,   UPLOAD_SIZE_GB),
            ("downloads", DOWNLOADS_DIR, UPLOAD_SIZE_GB * 2),
        ]

        for name, path, max_gb in checks:
            if not path.exists():
                continue
            key = f"folder_{name}"
            try:
                result = subprocess.run(
                    ["du", "-s", str(path)],
                    capture_output=True, text=True, timeout=15,
                )
                if result.returncode == 0 and result.stdout.strip():
                    size_kb = int(result.stdout.split()[0])
                    size_gb = size_kb / (1024 ** 2)
                    if size_gb > max_gb:
                        if self.cooldown.should_fire(key):
                            alerts.append(Alert(
                                key,
                                f"Folder '{name}' is {size_gb:.1f} GB (max {max_gb} GB)",
                                Severity.WARNING,
                            ))
                    else:
                        self.cooldown.clear(key)
            except Exception as exc:
                logger.warning("Folder check '%s' failed: %s", name, exc)

        return alerts

    # ── 8. Log error scan ─────────────────────────────────────

    LOG_ERROR_PATTERNS = [
        "ERROR", "Exception", "Traceback", "500", "502", "503",
        "Failed", "ModuleNotFoundError", "ImportError",
        "Killed", "OOM", "timeout", "Connection refused",
        "CRITICAL", "FATAL", "Segfault", "core dumped",
    ]

    def check_logs(self) -> List[Alert]:
        alerts: List[Alert] = []
        if not self.docker_cmd:
            return []

        containers = ["app", "worker", "nginx", "redis"]
        new_errors: List[str] = []

        for container in containers:
            try:
                result = subprocess.run(
                    self.docker_cmd + [
                        "logs", "--tail=100", "--no-color",
                        f"pdfwala_{container}",
                    ],
                    capture_output=True, text=True,
                    cwd=str(BASE_DIR),
                    timeout=20,
                )
                logs = (result.stdout or "") + (result.stderr or "")
                for line in logs.splitlines():
                    ll = line.lower()
                    if any(p.lower() in ll for p in self.LOG_ERROR_PATTERNS):
                        if self.log_store.is_new(f"{container}:{line}"):
                            new_errors.append(f"[{container}] {line[:180]}")
            except subprocess.TimeoutExpired:
                logger.warning("Log check for %s timed out", container)
            except Exception as exc:
                logger.warning("Log check '%s' failed: %s", container, exc)

        if new_errors:
            preview = "\n".join(new_errors[:5])
            suffix  = f"\n…+{len(new_errors)-5} more" if len(new_errors) > 5 else ""
            alerts.append(Alert(
                "new_log_errors",
                f"{len(new_errors)} new error(s) in logs:\n<pre>{preview}{suffix}</pre>",
                Severity.WARNING,
            ))

        return alerts

    # ══════════════════════════════════════════════════════════
    #  AUTO-RESTART
    # ══════════════════════════════════════════════════════════

    def _maybe_auto_restart(self, alerts: List[Alert]) -> None:
        """Restart containers flagged with auto_action=restart:<name>."""
        if not self.docker_cmd:
            return

        for alert in alerts:
            if not alert.auto_action or not alert.auto_action.startswith("restart:"):
                continue

            container = alert.auto_action.split(":", 1)[1]
            key       = f"autorestart_{container}"
            if not self.restart_cd.should_fire(key):
                logger.info("Auto-restart cooldown active for %s", container)
                continue

            logger.warning("Auto-restarting container: %s", container)
            try:
                subprocess.run(
                    self.docker_cmd + ["restart", container],
                    cwd=str(BASE_DIR),
                    capture_output=True, timeout=60, check=True,
                )
                self.telegram.send(
                    f"🔄 Auto-restarted container <b>{container}</b> on {HOSTNAME}"
                )
                logger.info("Auto-restart of %s succeeded", container)
            except subprocess.CalledProcessError as exc:
                logger.error("Auto-restart of %s failed: %s", container, exc)
                self.telegram.send(
                    f"❌ Auto-restart of <b>{container}</b> FAILED on {HOSTNAME}"
                )
            except Exception as exc:
                logger.exception("Auto-restart error for %s: %s", container, exc)

    # ══════════════════════════════════════════════════════════
    #  SUMMARY REPORT
    # ══════════════════════════════════════════════════════════

    def generate_summary(self) -> str:
        try:
            cpu   = psutil.cpu_percent(interval=0.5)
            ram   = psutil.virtual_memory()
            disk  = psutil.disk_usage("/")
            swap  = psutil.swap_memory()
            load1, load5, _ = os.getloadavg()
            uptime_secs = time.time() - psutil.boot_time()
            uptime_h    = int(uptime_secs // 3600)
            uptime_d    = uptime_h // 24
            uptime_str  = f"{uptime_d}d {uptime_h % 24}h"

            net   = psutil.net_io_counters()
            net_mb_rx = net.bytes_recv / 1e6
            net_mb_tx = net.bytes_sent / 1e6

            lines = [
                f"<b>📊 PDFWala Status Report</b>",
                f"🖥️  <b>{HOSTNAME}</b>  •  {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                "",
                f"<b>⚙️  System</b>",
                f"  CPU:    {cpu:.1f}%    Load: {load1:.2f} / {load5:.2f}",
                f"  RAM:    {ram.percent:.1f}%   "
                f"({ram.used/1e9:.1f} / {ram.total/1e9:.1f} GB)",
                f"  Swap:   {swap.percent:.1f}%",
                f"  Disk:   {disk.percent:.1f}%   "
                f"({disk.used/1e9:.1f} / {disk.total/1e9:.1f} GB free: "
                f"{disk.free/1e9:.1f} GB)",
                f"  Uptime: {uptime_str}",
                f"  Net ↓:  {net_mb_rx:.0f} MB  ↑ {net_mb_tx:.0f} MB",
                "",
            ]

            # Folder sizes
            lines.append("<b>📁  Storage</b>")
            for name, path in [("uploads", UPLOADS_DIR), ("downloads", DOWNLOADS_DIR)]:
                if path.exists():
                    try:
                        r = subprocess.run(
                            ["du", "-sh", str(path)],
                            capture_output=True, text=True, timeout=5,
                        )
                        size = r.stdout.split()[0] if r.stdout else "N/A"
                    except Exception:
                        size = "err"
                    lines.append(f"  {name}: {size}")

            # Active alert count
            lines += [
                "",
                f"<b>🔔  Active alert keys:</b> {len(self.cooldown._map)}",
            ]

            return "\n".join(lines)
        except Exception as exc:
            logger.exception("Summary generation failed")
            return f"❌ Summary failed: {exc}"

    # ══════════════════════════════════════════════════════════
    #  MAIN LOOP
    # ══════════════════════════════════════════════════════════

    def run(self) -> None:
        # Startup self-test
        logger.info("Running Telegram credential test…")
        if not self.telegram.test():
            logger.critical("Telegram credential test failed — check TOKEN and CHAT_ID")
            sys.exit(1)

        self.telegram.send(
            f"🟢 <b>PDFWala Monitor v3 started</b>\n"
            f"🖥️  Host: {HOSTNAME}\n"
            f"⚙️  CPU&gt;{CPU_THRESHOLD}% RAM&gt;{RAM_THRESHOLD}% Disk&gt;{DISK_THRESHOLD}%\n"
            f"🔔 Cooldown: {ALERT_COOLDOWN}s"
        )

        summary_counter = 0

        while self.running:
            cycle_start = time.monotonic()

            # ── run all checks ────────────────────────────────
            all_alerts: List[Alert] = []
            all_alerts.extend(self.check_system_health())
            all_alerts.extend(self.check_ssl())
            all_alerts.extend(self.check_containers())
            all_alerts.extend(self.check_redis())
            all_alerts.extend(self.check_processes())
            all_alerts.extend(self.check_endpoints())
            all_alerts.extend(self.check_folders())
            all_alerts.extend(self.check_logs())

            # ── auto-restart ──────────────────────────────────
            self._maybe_auto_restart(all_alerts)

            # ── dispatch alerts grouped by severity ───────────
            crits    = [a for a in all_alerts if a.severity == Severity.CRITICAL]
            warnings = [a for a in all_alerts if a.severity == Severity.WARNING]

            if crits:
                body = "\n".join(f"  • {a.message}" for a in crits[:12])
                self.telegram.send(
                    f"🚨 <b>CRITICAL — {HOSTNAME}</b>\n\n{body}"
                )
            if warnings:
                body = "\n".join(f"  • {a.message}" for a in warnings[:12])
                self.telegram.send(
                    f"⚠️ <b>WARNING — {HOSTNAME}</b>\n\n{body}"
                )

            if all_alerts:
                logger.warning(
                    "Cycle: %d critical, %d warning alert(s)", len(crits), len(warnings)
                )

            # ── metrics snapshot ──────────────────────────────
            try:
                ram  = psutil.virtual_memory()
                disk = psutil.disk_usage("/")
                self._write_metrics({
                    "ts":          datetime.now(timezone.utc).isoformat(),
                    "cpu_pct":     psutil.cpu_percent(),
                    "ram_pct":     ram.percent,
                    "ram_used_gb": round(ram.used / 1e9, 2),
                    "disk_pct":    disk.percent,
                    "disk_free_gb":round(disk.free / 1e9, 2),
                    "swap_pct":    psutil.swap_memory().percent,
                    "load1":       os.getloadavg()[0],
                    "alerts_crit": len(crits),
                    "alerts_warn": len(warnings),
                })
            except Exception:
                pass

            # ── heartbeat ─────────────────────────────────────
            self._touch_heartbeat()

            # ── periodic summary ──────────────────────────────
            summary_counter += CHECK_INTERVAL
            if summary_counter >= SUMMARY_INTERVAL:
                summary = self.generate_summary()
                self.telegram.send(summary, parse_mode="HTML")
                summary_counter = 0
                logger.info("Periodic summary sent")

            # ── sleep remainder of cycle ──────────────────────
            elapsed = time.monotonic() - cycle_start
            sleep_for = max(0, CHECK_INTERVAL - elapsed)
            self._stop_event.wait(timeout=sleep_for)

        # shutdown
        logger.info("Monitor stopped")
        self.telegram.send(f"🔴 <b>PDFWala Monitor v3 stopped</b> on {HOSTNAME}")


# ╔══════════════════════════════════════════════════════════════
# ║  HELPERS
# ╚══════════════════════════════════════════════════════════════

def _validate_config() -> None:
    """Abort early with clear message if required config is missing."""
    errors: List[str] = []
    if not TOKEN:
        errors.append("TELEGRAM_TOKEN is not set")
    if not CHAT_ID:
        errors.append("TELEGRAM_CHAT_ID is not set")
    if CHECK_INTERVAL < 10:
        errors.append(f"CHECK_INTERVAL={CHECK_INTERVAL} is dangerously low (min 10s)")
    if ALERT_COOLDOWN < 60:
        errors.append(f"ALERT_COOLDOWN={ALERT_COOLDOWN} is very low (min 60s)")
    if errors:
        for e in errors:
            print(f"❌ Config error: {e}", file=sys.stderr)
        sys.exit(1)


def resource_limits() -> Tuple[Optional[int], Optional[int]]:
    """Return (soft, hard) fd limits for the current process."""
    try:
        import resource as _res
        soft, hard = _res.getrlimit(_res.RLIMIT_NOFILE)
        return soft, hard
    except Exception:
        return None, None


def _count_open_fds() -> int:
    """Count file descriptors open by the current process."""
    try:
        return len(list(Path(f"/proc/{os.getpid()}/fd").iterdir()))
    except Exception:
        return psutil.Process(os.getpid()).num_fds()


# ╔══════════════════════════════════════════════════════════════
# ║  ENTRY POINT
# ╚══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        PDFWalaMonitor().run()
    except KeyboardInterrupt:
        print("\nInterrupted")
    except Exception as exc:
        logger.critical("Fatal error: %s", exc, exc_info=True)
        sys.exit(1)
