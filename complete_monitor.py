#!/usr/bin/env python3
"""
PDFWala Production Monitor v3
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors:
  • CPU / RAM / Disk / Swap / Load / File descriptors
  • Memory leak trend detection
  • Zombie process detection
  • Disk I/O saturation
  • SSL certificate expiry (warn <30d, critical <7d)
  • Docker container health (4 containers)
  • Redis PING via docker exec
  • HTTP endpoints (homepage + API health)
  • Uploads / Downloads folder size
  • Docker log error scanning with dedup
  • Auto-restart failed containers (5 min cooldown)
  • Telegram alerts with severity levels + rate limiter
  • JSON metrics snapshot + heartbeat file each cycle
  • 6-hour summary report
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

# ── optional deps ─────────────────────────────────────────────
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
    print("❌ psutil required: pip install psutil", file=sys.stderr)
    sys.exit(1)

try:
    import requests
except ImportError:
    print("❌ requests required: pip install requests", file=sys.stderr)
    sys.exit(1)


# ╔══════════════════════════════════════════════════════════════
# ║  CONFIGURATION  (all overridable via .env)
# ╚══════════════════════════════════════════════════════════════

def _ei(k, d):
    try: return int(os.getenv(k, d))
    except ValueError: return d

def _ef(k, d):
    try: return float(os.getenv(k, d))
    except ValueError: return d

# Telegram
TOKEN   = os.getenv("TELEGRAM_TOKEN",   "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

# Thresholds
CPU_THRESHOLD    = _ei("CPU_THRESHOLD",    90)
RAM_THRESHOLD    = _ei("RAM_THRESHOLD",    90)
DISK_THRESHOLD   = _ei("DISK_THRESHOLD",   90)
SWAP_THRESHOLD   = _ei("SWAP_THRESHOLD",   50)
FD_THRESHOLD     = _ei("FD_THRESHOLD",     80)    # % of system fd limit
LOAD_MULTIPLIER  = _ef("LOAD_MULTIPLIER",  1.5)
UPLOAD_SIZE_GB   = _ef("UPLOAD_SIZE_GB",   10.0)
DOWNLOAD_SIZE_GB = _ef("DOWNLOAD_SIZE_GB", 20.0)
DISK_IO_PCT      = _ei("DISK_IO_BUSY_PCT", 90)
SSL_WARN_DAYS    = _ei("SSL_WARN_DAYS",    30)
SSL_CRIT_DAYS    = _ei("SSL_CRIT_DAYS",     7)
MEM_TREND_CYCLES = _ei("MEM_TREND_CYCLES",  5)
MEM_TREND_DELTA  = _ef("MEM_TREND_DELTA",  3.0)   # % per sample

# Timing
CHECK_INTERVAL        = _ei("CHECK_INTERVAL",        60)
SUMMARY_INTERVAL      = _ei("SUMMARY_INTERVAL",   21600)  # 6 hours
ALERT_COOLDOWN        = _ei("ALERT_COOLDOWN",       1800)  # 30 min
AUTO_RESTART_COOLDOWN = _ei("AUTO_RESTART_COOLDOWN", 300)  # 5 min

# Telegram rate limit
TG_RATE_MAX    = _ei("TG_RATE_MAX",    20)
TG_RATE_WINDOW = _ei("TG_RATE_WINDOW", 60)

# Paths
BASE_DIR       = Path(os.getenv("BASE_DIR", "/home/opc/pdfwala"))
LOG_FILE       = BASE_DIR / "monitor.log"
UPLOADS_DIR    = BASE_DIR / "uploads"
DOWNLOADS_DIR  = BASE_DIR / "downloads"
METRICS_FILE   = BASE_DIR / "monitor_metrics.json"
HEARTBEAT_FILE = BASE_DIR / "monitor_heartbeat"
LOG_HASH_FILE  = BASE_DIR / "monitor_log_hashes.json"

# Containers
EXPECTED_CONTAINERS = [
    "pdfwala_app",
    "pdfwala_worker",
    "pdfwala_nginx",
    "pdfwala_redis",
]

# Endpoints  (name, url, expected_code, is_critical)
ENDPOINTS: List[Tuple[str, str, int, bool]] = [
    ("Homepage",   "https://npkpadala.com/pdfwala/", 200, True),
    ("API Health", "https://npkpadala.com/api/health", 200, True),
]

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
log = logging.getLogger("pdfwala.monitor")


# ╔══════════════════════════════════════════════════════════════
# ║  SEVERITY + ALERT
# ╚══════════════════════════════════════════════════════════════

class Sev:
    WARN = "WARNING"
    CRIT = "CRITICAL"

_EMOJI = {Sev.WARN: "⚠️", Sev.CRIT: "🚨"}


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
# ║  TELEGRAM  (thread-safe, rate-limited)
# ╚══════════════════════════════════════════════════════════════

class Telegram:
    def __init__(self, token: str, chat_id: str):
        self._token   = token
        self._chat_id = chat_id
        self._lock    = threading.Lock()
        self._sends: Deque[float] = deque()
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"

    def _under_limit(self) -> bool:
        now = time.monotonic()
        while self._sends and now - self._sends[0] > TG_RATE_WINDOW:
            self._sends.popleft()
        return len(self._sends) < TG_RATE_MAX

    def send(self, text: str, parse_mode: str = "HTML") -> bool:
        if len(text) > 4000:
            text = text[:3900] + "\n…[truncated]"
        with self._lock:
            if not self._under_limit():
                log.warning("Telegram rate limit — message suppressed")
                return False
            self._sends.append(time.monotonic())
        for attempt in range(3):
            try:
                r = requests.post(
                    self._url,
                    json={"chat_id": self._chat_id, "text": text,
                          "parse_mode": parse_mode},
                    timeout=15,
                )
                if r.status_code == 200:
                    return True
                log.error("Telegram %s: %s", r.status_code, r.text[:100])
            except Exception as e:
                log.error("Telegram attempt %d: %s", attempt + 1, e)
                time.sleep(2 ** attempt)
        return False

    def test(self) -> bool:
        return self.send("🔍 PDFWala Monitor — credential test OK")


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
# ║  LOG HASH STORE  (persists seen errors across restarts)
# ╚══════════════════════════════════════════════════════════════

class LogHashStore:
    MAX = 2000

    def __init__(self, path: Path):
        self._path   = path
        self._hashes: Set[str] = set()
        self._lock   = threading.Lock()
        self._load()

    def _load(self):
        try:
            if self._path.exists():
                d = json.loads(self._path.read_text())
                self._hashes = set(d.get("hashes", []))
                log.info("Loaded %d log hashes", len(self._hashes))
        except Exception as e:
            log.warning("Log hash load failed: %s", e)

    def _save(self):
        try:
            if len(self._hashes) > self.MAX:
                self._hashes = set(list(self._hashes)[-self.MAX:])
            self._path.write_text(json.dumps({"hashes": list(self._hashes)}))
        except Exception as e:
            log.warning("Log hash save failed: %s", e)

    def is_new(self, raw: str) -> bool:
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

class Monitor:

    def __init__(self):
        _validate_config()

        self.running     = True
        self._stop       = threading.Event()
        self.tg          = Telegram(TOKEN, CHAT_ID)
        self.cd          = Cooldown(ALERT_COOLDOWN)
        self.restart_cd  = Cooldown(AUTO_RESTART_COOLDOWN)
        self.log_store   = LogHashStore(LOG_HASH_FILE)
        self.docker_cmd  = self._find_docker_compose()

        self._mem_samples: Deque[float] = deque(maxlen=MEM_TREND_CYCLES + 1)
        self._prev_io: Optional[object] = None
        self._prev_io_ts: float = 0.0

        signal.signal(signal.SIGTERM, self._on_shutdown)
        signal.signal(signal.SIGINT,  self._on_shutdown)
        signal.signal(signal.SIGHUP,  lambda s, f: log.info("SIGHUP received"))

        log.info("PDFWala Monitor v3 initialised on %s", HOSTNAME)

    # ── signals ───────────────────────────────────────────────

    def _on_shutdown(self, sig, _):
        log.info("Signal %s — shutting down", sig)
        self.running = False
        self._stop.set()

    # ── docker compose detection ──────────────────────────────

    def _find_docker_compose(self) -> Optional[List[str]]:
        for cmd in (["docker", "compose"], ["docker-compose"]):
            try:
                subprocess.run(cmd + ["version"],
                               capture_output=True, check=True, timeout=5)
                log.info("Docker Compose: %s", " ".join(cmd))
                return cmd
            except Exception:
                pass
        log.warning("Docker Compose not found — container checks disabled")
        return None

    # ── utils ─────────────────────────────────────────────────

    def _heartbeat(self):
        try:
            HEARTBEAT_FILE.write_text(datetime.now(timezone.utc).isoformat())
        except Exception:
            pass

    def _write_metrics(self, m: dict):
        try:
            METRICS_FILE.write_text(json.dumps(m, indent=2, default=str))
        except Exception:
            pass

    # ══════════════════════════════════════════════════════════
    #  CHECK METHODS
    # ══════════════════════════════════════════════════════════

    # 1 ── System health ───────────────────────────────────────

    def check_system(self) -> List[Alert]:
        alerts: List[Alert] = []
        try:
            # CPU
            cpu = psutil.cpu_percent(interval=1)
            k = "cpu"
            if cpu > CPU_THRESHOLD:
                if self.cd.should_fire(k):
                    alerts.append(Alert(k,
                        f"CPU at {cpu:.1f}% (threshold {CPU_THRESHOLD}%)",
                        Sev.CRIT if cpu > 95 else Sev.WARN))
            else:
                self.cd.clear(k)

            # RAM
            ram = psutil.virtual_memory()
            k = "ram"
            if ram.percent > RAM_THRESHOLD:
                if self.cd.should_fire(k):
                    alerts.append(Alert(k,
                        f"RAM {ram.percent:.1f}% "
                        f"({ram.used/1e9:.1f}/{ram.total/1e9:.1f} GB)",
                        Sev.CRIT if ram.percent > 95 else Sev.WARN))
            else:
                self.cd.clear(k)

            # Memory trend (leak detection)
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

            # Swap
            swap = psutil.swap_memory()
            k = "swap"
            if swap.percent > SWAP_THRESHOLD:
                if self.cd.should_fire(k):
                    alerts.append(Alert(k,
                        f"Swap {swap.percent:.1f}% used", Sev.WARN))
            else:
                self.cd.clear(k)

            # Disk
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

            # Load average
            load1, _, _ = os.getloadavg()
            cpus = psutil.cpu_count() or 1
            k = "load"
            if load1 > cpus * LOAD_MULTIPLIER:
                if self.cd.should_fire(k):
                    alerts.append(Alert(k,
                        f"Load avg {load1:.2f} on {cpus} CPUs "
                        f"(threshold ×{LOAD_MULTIPLIER})", Sev.WARN))
            else:
                self.cd.clear(k)

            # File descriptors
            try:
                import resource as _res
                soft, _ = _res.getrlimit(_res.RLIMIT_NOFILE)
                try:
                    fd_open = len(list(Path(f"/proc/{os.getpid()}/fd").iterdir()))
                except Exception:
                    fd_open = psutil.Process(os.getpid()).num_fds()
                k = "fd"
                if soft and (fd_open / soft) > FD_THRESHOLD / 100:
                    if self.cd.should_fire(k):
                        alerts.append(Alert(k,
                            f"File descriptors {fd_open}/{soft} "
                            f"({fd_open*100//soft}% of limit)", Sev.WARN))
                else:
                    self.cd.clear(k)
            except Exception:
                pass

            # Zombie processes
            zombies = [p for p in psutil.process_iter(["status"])
                       if p.info.get("status") == psutil.STATUS_ZOMBIE]
            k = "zombies"
            if zombies:
                if self.cd.should_fire(k):
                    alerts.append(Alert(k,
                        f"{len(zombies)} zombie process(es) detected",
                        Sev.WARN))
            else:
                self.cd.clear(k)

            # Disk I/O saturation
            self._check_disk_io(alerts)

        except Exception as e:
            log.exception("System check error")
            alerts.append(Alert("sys_err", f"System check failed: {e}", Sev.WARN))

        return alerts

    def _check_disk_io(self, alerts: List[Alert]):
        try:
            now = time.monotonic()
            c   = psutil.disk_io_counters()
            if self._prev_io is not None and c:
                elapsed  = now - self._prev_io_ts
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
        except Exception:
            pass  # busy_time not on all kernels

    # 2 ── SSL ─────────────────────────────────────────────────

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
                    cert     = s.getpeercert()
                    exp_str  = cert.get("notAfter", "")
                    exp_dt   = datetime.strptime(
                        exp_str, "%b %d %H:%M:%S %Y %Z"
                    ).replace(tzinfo=timezone.utc)
                    days     = (exp_dt - datetime.now(timezone.utc)).days
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
                    alerts.append(Alert(k, f"SSL error {host}: {e}", Sev.CRIT))
            except Exception as e:
                log.warning("SSL check %s: %s", host, e)
        return alerts

    # 3 ── Containers ──────────────────────────────────────────

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
                    alerts.append(Alert(k, "Docker daemon not running", Sev.CRIT))
                return alerts
            self.cd.clear(k)

            r = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"],
                capture_output=True, text=True, timeout=10,
            )
            running: Dict[str, str] = {}
            for line in r.stdout.strip().splitlines():
                parts = line.split("\t", 1)
                if len(parts) == 2:
                    running[parts[0]] = parts[1]

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
            alerts.append(Alert("docker_timeout",
                                "Docker check timed out", Sev.WARN))
        except Exception as e:
            log.exception("Container check error")
            alerts.append(Alert("ct_err",
                                f"Container check failed: {e}", Sev.WARN))
        return alerts

    # 4 ── Redis  (docker exec redis-cli ping) ─────────────────

    def check_redis(self) -> List[Alert]:
        alerts: List[Alert] = []
        k = "redis"
        try:
            r = subprocess.run(
                ["docker", "exec", "pdfwala_redis", "redis-cli", "ping"],
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
            if self.cd.should_fire(k):
                alerts.append(Alert(k, "Redis check timed out", Sev.CRIT))
        except Exception as e:
            if self.cd.should_fire(k):
                alerts.append(Alert(k, f"Redis check failed: {e}", Sev.CRIT))
        return alerts

    # 5 ── Processes ───────────────────────────────────────────

    def check_processes(self) -> List[Alert]:
        alerts: List[Alert] = []
        names = psutil.process_iter(["name"])
        running = {p.info["name"] or "" for p in names}
        for proc, key in [("gunicorn", "proc_gunicorn"),
                          ("celery",   "proc_celery")]:
            found = any(proc in n for n in running)
            if not found:
                if self.cd.should_fire(key):
                    alerts.append(Alert(key,
                        f"Process '{proc}' not found on host", Sev.WARN))
            else:
                self.cd.clear(key)
        return alerts

    # 6 ── Endpoints ───────────────────────────────────────────

    def check_endpoints(self) -> List[Alert]:
        alerts: List[Alert] = []
        hdrs = {"User-Agent": "PDFWala-Monitor/3.0"}
        for name, url, expected, critical in ENDPOINTS:
            k     = f"ep_{name.replace(' ','_')}"
            k_sl  = f"{k}_slow"
            try:
                t0   = time.monotonic()
                resp = requests.get(url, timeout=10,
                                    headers=hdrs, allow_redirects=True)
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
                if self.cd.should_fire(k):
                    alerts.append(Alert(k, f"{name} timed out",
                        Sev.CRIT if critical else Sev.WARN))
            except Exception as e:
                if self.cd.should_fire(k):
                    alerts.append(Alert(k,
                        f"{name} unreachable: {str(e)[:80]}",
                        Sev.CRIT if critical else Sev.WARN))
        return alerts

    # 7 ── Folder sizes ────────────────────────────────────────

    def check_folders(self) -> List[Alert]:
        alerts: List[Alert] = []
        for name, path, max_gb in [
            ("uploads",   UPLOADS_DIR,   UPLOAD_SIZE_GB),
            ("downloads", DOWNLOADS_DIR, DOWNLOAD_SIZE_GB),
        ]:
            if not path.exists():
                continue
            k = f"folder_{name}"
            try:
                r = subprocess.run(["du", "-s", str(path)],
                                   capture_output=True, text=True, timeout=20)
                if r.returncode == 0 and r.stdout.strip():
                    size_gb = int(r.stdout.split()[0]) / (1024 ** 2)
                    if size_gb > max_gb:
                        if self.cd.should_fire(k):
                            alerts.append(Alert(k,
                                f"Folder '{name}' is {size_gb:.1f} GB "
                                f"(max {max_gb} GB)", Sev.WARN))
                    else:
                        self.cd.clear(k)
            except Exception as e:
                log.warning("Folder check '%s': %s", name, e)
        return alerts

    # 8 ── Log scanning ────────────────────────────────────────

    LOG_PATTERNS = [
        "ERROR", "Exception", "Traceback", "500", "502", "503",
        "Failed", "ModuleNotFoundError", "ImportError",
        "Killed", "OOM", "timeout", "Connection refused",
        "CRITICAL", "FATAL", "Segfault", "core dumped",
    ]

    def check_logs(self) -> List[Alert]:
        alerts: List[Alert] = []
        if not self.docker_cmd:
            return []

        new_errors: List[str] = []
        for ct in ["app", "worker", "nginx", "redis"]:
            try:
                r = subprocess.run(
                    self.docker_cmd + ["logs", "--tail=100",
                                       "--no-color", f"pdfwala_{ct}"],
                    capture_output=True, text=True,
                    cwd=str(BASE_DIR), timeout=20,
                )
                logs = (r.stdout or "") + (r.stderr or "")
                for line in logs.splitlines():
                    ll = line.lower()
                    if any(p.lower() in ll for p in self.LOG_PATTERNS):
                        if self.log_store.is_new(f"{ct}:{line}"):
                            new_errors.append(f"[{ct}] {line[:180]}")
            except subprocess.TimeoutExpired:
                log.warning("Log check %s timed out", ct)
            except Exception as e:
                log.warning("Log check '%s': %s", ct, e)

        if new_errors:
            preview = "\n".join(new_errors[:5])
            suffix  = f"\n…+{len(new_errors)-5} more" if len(new_errors) > 5 else ""
            alerts.append(Alert("log_errors",
                f"{len(new_errors)} new error(s) in logs:\n"
                f"<pre>{preview}{suffix}</pre>",
                Sev.WARN))
        return alerts

    # ══════════════════════════════════════════════════════════
    #  AUTO-RESTART
    # ══════════════════════════════════════════════════════════

    def _auto_restart(self, alerts: List[Alert]):
        if not self.docker_cmd:
            return
        for a in alerts:
            if not (a.auto_action and a.auto_action.startswith("restart:")):
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
                    f"🔄 Auto-restarted <b>{name}</b> on {HOSTNAME}")
                log.info("Auto-restart OK: %s", name)
            except Exception as e:
                log.error("Auto-restart FAILED %s: %s", name, e)
                self.tg.send(
                    f"❌ Auto-restart FAILED <b>{name}</b> on {HOSTNAME}")

    # ══════════════════════════════════════════════════════════
    #  SUMMARY REPORT
    # ══════════════════════════════════════════════════════════

    def summary(self) -> str:
        try:
            cpu  = psutil.cpu_percent(interval=0.5)
            ram  = psutil.virtual_memory()
            disk = psutil.disk_usage("/")
            swap = psutil.swap_memory()
            l1, l5, _ = os.getloadavg()
            up_h  = int((time.time() - psutil.boot_time()) // 3600)
            net   = psutil.net_io_counters()

            lines = [
                "<b>📊 PDFWala Status Report</b>",
                f"🖥️  <b>{HOSTNAME}</b>  •  "
                f"{datetime.now().strftime('%Y-%m-%d %H:%M')}",
                "",
                "<b>⚙️  System</b>",
                f"  CPU : {cpu:.1f}%   Load: {l1:.2f}/{l5:.2f}",
                f"  RAM : {ram.percent:.1f}%  "
                f"({ram.used/1e9:.1f}/{ram.total/1e9:.1f} GB)",
                f"  Swap: {swap.percent:.1f}%",
                f"  Disk: {disk.percent:.1f}%  "
                f"(free {disk.free/1e9:.1f} GB)",
                f"  Up  : {up_h//24}d {up_h%24}h",
                f"  Net : ↓{net.bytes_recv/1e6:.0f}MB  "
                f"↑{net.bytes_sent/1e6:.0f}MB",
                "",
                "<b>📁  Storage</b>",
            ]
            for name, path in [("uploads",UPLOADS_DIR),
                                ("downloads",DOWNLOADS_DIR)]:
                if path.exists():
                    try:
                        r    = subprocess.run(["du","-sh",str(path)],
                                              capture_output=True,
                                              text=True, timeout=5)
                        size = r.stdout.split()[0] if r.stdout else "N/A"
                    except Exception:
                        size = "err"
                    lines.append(f"  {name}: {size}")
            lines += ["",
                      f"<b>🔔  Active cooldown keys:</b> "
                      f"{self.cd.active_count()}"]
            return "\n".join(lines)
        except Exception as e:
            log.exception("Summary failed")
            return f"❌ Summary failed: {e}"

    # ══════════════════════════════════════════════════════════
    #  MAIN LOOP
    # ══════════════════════════════════════════════════════════

    def run(self):
        log.info("Running Telegram credential test…")
        if not self.tg.test():
            log.critical("Telegram test FAILED — check TOKEN / CHAT_ID")
            sys.exit(1)

        self.tg.send(
            f"🟢 <b>PDFWala Monitor v3 started</b>\n"
            f"🖥️  {HOSTNAME}\n"
            f"⚙️  CPU&gt;{CPU_THRESHOLD}%  "
            f"RAM&gt;{RAM_THRESHOLD}%  "
            f"Disk&gt;{DISK_THRESHOLD}%\n"
            f"🔔 Cooldown: {ALERT_COOLDOWN}s  "
            f"Interval: {CHECK_INTERVAL}s"
        )

        summary_acc = 0

        while self.running:
            t0 = time.monotonic()

            # ── run all checks ────────────────────────────────
            all_alerts: List[Alert] = []
            for fn in (
                self.check_system,
                self.check_ssl,
                self.check_containers,
                self.check_redis,
                self.check_processes,
                self.check_endpoints,
                self.check_folders,
                self.check_logs,
            ):
                try:
                    all_alerts.extend(fn())
                except Exception as e:
                    log.exception("Check %s failed: %s", fn.__name__, e)

            # ── auto-restart ──────────────────────────────────
            self._auto_restart(all_alerts)

            # ── dispatch by severity ──────────────────────────
            crits = [a for a in all_alerts if a.severity == Sev.CRIT]
            warns = [a for a in all_alerts if a.severity == Sev.WARN]

            if crits:
                body = "\n".join(f"  • {a.message}" for a in crits[:12])
                self.tg.send(f"🚨 <b>CRITICAL — {HOSTNAME}</b>\n\n{body}")
            if warns:
                body = "\n".join(f"  • {a.message}" for a in warns[:12])
                self.tg.send(f"⚠️ <b>WARNING — {HOSTNAME}</b>\n\n{body}")

            if all_alerts:
                log.warning("Cycle: %d critical, %d warning",
                            len(crits), len(warns))

            # ── metrics snapshot ──────────────────────────────
            try:
                ram  = psutil.virtual_memory()
                disk = psutil.disk_usage("/")
                self._write_metrics({
                    "ts":           datetime.now(timezone.utc).isoformat(),
                    "cpu_pct":      psutil.cpu_percent(),
                    "ram_pct":      ram.percent,
                    "ram_used_gb":  round(ram.used / 1e9, 2),
                    "disk_pct":     disk.percent,
                    "disk_free_gb": round(disk.free / 1e9, 2),
                    "swap_pct":     psutil.swap_memory().percent,
                    "load1":        round(os.getloadavg()[0], 2),
                    "alerts_crit":  len(crits),
                    "alerts_warn":  len(warns),
                })
            except Exception:
                pass

            # ── heartbeat ─────────────────────────────────────
            self._heartbeat()

            # ── 6-hour summary ────────────────────────────────
            summary_acc += CHECK_INTERVAL
            if summary_acc >= SUMMARY_INTERVAL:
                self.tg.send(self.summary())
                summary_acc = 0
                log.info("6-hour summary sent")

            # ── sleep ─────────────────────────────────────────
            elapsed   = time.monotonic() - t0
            sleep_for = max(0, CHECK_INTERVAL - elapsed)
            self._stop.wait(timeout=sleep_for)

        log.info("Monitor stopped")
        self.tg.send(f"🔴 <b>PDFWala Monitor stopped</b> on {HOSTNAME}")


# ╔══════════════════════════════════════════════════════════════
# ║  HELPERS
# ╚══════════════════════════════════════════════════════════════

def _validate_config():
    errs = []
    if not TOKEN:
        errs.append("TELEGRAM_TOKEN not set")
    if not CHAT_ID:
        errs.append("TELEGRAM_CHAT_ID not set")
    if CHECK_INTERVAL < 10:
        errs.append(f"CHECK_INTERVAL={CHECK_INTERVAL} too low (min 10)")
    if ALERT_COOLDOWN < 60:
        errs.append(f"ALERT_COOLDOWN={ALERT_COOLDOWN} too low (min 60)")
    if errs:
        for e in errs:
            print(f"❌ Config: {e}", file=sys.stderr)
        sys.exit(1)


# ╔══════════════════════════════════════════════════════════════
# ║  ENTRY POINT
# ╚══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    try:
        Monitor().run()
    except KeyboardInterrupt:
        print("\nInterrupted")
    except Exception as e:
        log.critical("Fatal: %s", e, exc_info=True)
        sys.exit(1)
