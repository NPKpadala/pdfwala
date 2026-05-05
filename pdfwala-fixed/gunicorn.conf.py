"""
PDFWala Enterprise V13.1 (PATCHED)
gunicorn.conf.py — Production WSGI server configuration.

FIXES:
  - Raised worker_connections and backlog for high-load scenarios
  - Timeout extended to 300s to match heavy-workload subprocess timeouts
  - keepalive raised to 75s for long-running connection reuse
  - Added worker_tmp_dir for faster worker tmpfiles (if /dev/shm available)
"""

import os

# Server socket
bind    = "0.0.0.0:5000"
backlog = 4096

# Worker processes — gthread handles I/O-bound workloads well
workers      = int(os.environ.get("GUNICORN_WORKERS", 4))
worker_class = "gthread"
threads      = int(os.environ.get("GUNICORN_THREADS", 8))

# Timeouts — sync operations have 300s subprocess timeout; match here
timeout          = 300
graceful_timeout = 60
keepalive        = 75

max_requests        = 2000
max_requests_jitter = 200

# Worker temp dir — use RAM-backed tmpfs if available for better performance
worker_tmp_dir = "/dev/shm" if os.path.isdir("/dev/shm") else None

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

# Server mechanics
daemon          = False
pidfile         = None
umask           = 0
user            = None
group           = None

# SSL (handled by nginx)
keyfile  = None
certfile = None


def on_starting(server):
    print(f"PDFWala Enterprise V13.1 starting... workers={workers} threads={threads} timeout={timeout}s")


def when_ready(server):
    print("PDFWala Enterprise V13.1 ready")


def on_exit(server):
    print("PDFWala Enterprise V13.1 shutting down")
