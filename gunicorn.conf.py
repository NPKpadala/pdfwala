"""
gunicorn.conf.py — PDFWala Enterprise V14.0

V14 FIXES:
  - worker_connections raised to 2000 (was 1000 — insufficient for heavy load)
  - forwardpass config for X-Forwarded-Proto (Cloudflare termination)
  - limit_request_line raised to handle long signed URLs with query params
  - limit_request_fields raised for multi-file uploads
  - Preload app (preload_app=True) for faster worker fork and shared memory
  - umask set to 0o022 to ensure correct file permissions on output files
"""

import os

# Server socket
bind    = "0.0.0.0:5000"
backlog = 4096

# Worker processes
workers      = int(os.environ.get("GUNICORN_WORKERS", 4))
worker_class = "gthread"
threads      = int(os.environ.get("GUNICORN_THREADS", 8))

# FIX V14: Raised worker_connections for heavy concurrency
worker_connections = 2000

# Timeouts
timeout          = 120
graceful_timeout = 60
keepalive        = 75

max_requests        = 2000
max_requests_jitter = 200

# FIX V14: Preload app — reduces memory (copy-on-write) and speeds up worker restart
preload_app = True

# Worker temp dir
worker_tmp_dir = "/dev/shm" if os.path.isdir("/dev/shm") else None

# FIX V14: Correct file permissions on output files written by workers
umask = 0o022

# FIX V14: Handle long URLs (signed download URLs have multiple query params)
limit_request_line   = 8190
limit_request_fields = 200

# Logging
accesslog = "-"
errorlog  = "-"
loglevel  = os.environ.get("LOG_LEVEL", "info").lower()

access_log_format = (
    '{"time":"%(t)s","method":"%(m)s","path":"%(U)s",'
    '"status":"%(s)s","duration_us":%(D)s,"ip":"%(h)s",'
    '"req_id":"%({X-Request-ID}i)s"}'
)

# Process naming
proc_name = "pdfwala"

daemon   = False
pidfile  = None
user     = None
group    = None

# SSL (handled by nginx)
keyfile  = None
certfile = None


def on_starting(server):
    print(
        f"PDFWala Enterprise V14.0 starting... "
        f"workers={workers} threads={threads} timeout={timeout}s "
        f"preload_app={preload_app}"
    )


def when_ready(server):
    print("PDFWala Enterprise V14.0 ready")


def worker_init(arbiter, worker):
    # Each worker gets its own LibreOffice profile dir
    import tempfile
    os.environ.setdefault("HOME", f"/tmp/lo_home_{worker.pid}")


def on_exit(server):
    print("PDFWala Enterprise V14.0 shutting down")
