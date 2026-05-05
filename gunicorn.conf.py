"""
PDFWala Enterprise V13.0.0
gunicorn.conf.py — Production WSGI server configuration.
"""

import os

# Server socket
bind    = "0.0.0.0:5000"
backlog = 2048

# Worker processes
workers      = int(os.environ.get("GUNICORN_WORKERS", 4))
worker_class = "gthread"
threads      = int(os.environ.get("GUNICORN_THREADS", 8))

# Timeouts — large files go async via Celery, so 120s is sufficient here
timeout          = 120
graceful_timeout = 120
keepalive        = 5

max_requests        = 1000
max_requests_jitter = 100

# Logging
accesslog = "-"
errorlog  = "-"
loglevel  = os.environ.get("LOG_LEVEL", "info").lower()

# JSON log format for structured logging
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
tmp_upload_dir  = None

# SSL (handled by nginx)
keyfile  = None
certfile = None

# Lifecycle hooks
def on_starting(server):
    print("🚀 PDFWala Enterprise V13.0.0 starting…")
    print(f"   workers={workers}  threads={threads}  timeout={timeout}s")

def when_ready(server):
    print("✅ PDFWala Enterprise V13.0.0 ready")

def on_exit(server):
    print("🛑 PDFWala Enterprise V13.0.0 shutting down")
