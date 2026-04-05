# gunicorn.conf.py — Production optimized for PDF processing SaaS
import multiprocessing

# ─── Binding ──────────────────────────────────────────────────────
bind = "0.0.0.0:5000"

# ─── Workers ──────────────────────────────────────────────────────
# PDF processing is CPU + I/O bound.
# Use (2 × CPU) + 1 workers, max 8 to avoid OOM on large files.
workers = min((multiprocessing.cpu_count() * 2) + 1, 8)

# Threads per worker — helps with I/O wait (LibreOffice, disk writes)
threads = 2

# Use sync worker — avoids async issues with fitz/PyMuPDF (not thread-safe across greenlets)
worker_class = "sync"

# ─── Timeouts ─────────────────────────────────────────────────────
# LibreOffice + large PDF processing can take 60-90s
timeout = 120
graceful_timeout = 30
keepalive = 5

# ─── Limits ───────────────────────────────────────────────────────
# 200 MB max upload (match Flask config)
limit_request_line = 8190
limit_request_fields = 200
limit_request_field_size = 209715200  # 200 MB

# ─── Worker Lifecycle ─────────────────────────────────────────────
# Restart workers after N requests to prevent memory bloat from PyMuPDF
max_requests = 500
max_requests_jitter = 50

# ─── Logging ──────────────────────────────────────────────────────
accesslog = "-"
errorlog  = "-"
loglevel  = "info"
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)sµs'

# ─── Process Name ─────────────────────────────────────────────────
proc_name = "pdfwala"

# ─── Preload ──────────────────────────────────────────────────────
# Preload app to share memory across workers (faster startup, less RAM)
preload_app = True

# ─── Worker tmp dir ───────────────────────────────────────────────
# Use /dev/shm for worker heartbeat files (faster on Linux)
worker_tmp_dir = "/dev/shm"
