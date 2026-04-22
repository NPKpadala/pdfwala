"""
PDFWala Enterprise V11.0.0
gunicorn.conf.py — Production WSGI server configuration.

Key changes from V10:
  - timeout / graceful_timeout raised to 900s (15 min) for large-file sync fallback
  - workers and threads driven from environment variables
  - VERSION updated to 11.0.0 in startup messages
"""

import os

# ── Server socket ──────────────────────────────────────────────────────────────
bind    = "0.0.0.0:5000"
backlog = 2048

# ── Worker processes ───────────────────────────────────────────────────────────
workers      = int(os.environ.get("GUNICORN_WORKERS", 4))
worker_class = "gthread"
threads      = int(os.environ.get("GUNICORN_THREADS", 8))

# CRITICAL: 900 s = 15 minutes — required for large-file synchronous fallback
# when Celery is unavailable.  Lowering this will cause 502 on big conversions.
timeout         = 900
graceful_timeout = 900
keepalive       = 5

max_requests        = 1000
max_requests_jitter = 100

# ── Logging ────────────────────────────────────────────────────────────────────
accesslog = "-"
errorlog  = "-"
loglevel  = "info"
# JSON format for log aggregators (Loki, CloudWatch, etc.)
access_log_format = (
    '{"time":"%(t)s","method":"%(m)s","path":"%(U)s",'
    '"status":"%(s)s","duration_us":%(D)s,"ip":"%(h)s",'
    '"req_id":"%(({X-Request-ID}i)s"}'
)

# ── Process naming ─────────────────────────────────────────────────────────────
proc_name = "pdfwala"

# ── Server mechanics ───────────────────────────────────────────────────────────
daemon          = False
pidfile         = None
umask           = 0
user            = None
group           = None
tmp_upload_dir  = None

# ── SSL (disabled — handled by nginx reverse proxy) ────────────────────────────
keyfile  = None
certfile = None

# ── Lifecycle hooks ────────────────────────────────────────────────────────────
def on_starting(server):
    print("🚀 PDFWala Enterprise V11.0.0 starting…")
    print(f"   workers={workers}  threads={threads}  timeout={timeout}s")

def on_reload(server):
    print("🔄 PDFWala Enterprise V11.0.0 reloading…")

def when_ready(server):
    print("✅ PDFWala Enterprise V11.0.0 ready to accept connections")

def on_exit(server):
    print("🛑 PDFWala Enterprise V11.0.0 shutting down")

def worker_exit(server, worker):
    print(f"👷 Worker {worker.pid} exited")
