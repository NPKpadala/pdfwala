# Gunicorn configuration for PDFWala V10.0
import multiprocessing

# Server socket
bind = "0.0.0.0:5000"
backlog = 2048

# Worker processes
workers = 4
worker_class = "gthread"
threads = 8
timeout = 300
keepalive = 5
max_requests = 1000
max_requests_jitter = 100

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"
access_log_format = '{"time":"%(t)s","method":"%(m)s","path":"%(U)s","status":"%(s)s","duration":%(D)s,"ip":"%(h)s"}'

# Process naming
proc_name = "pdfwala"

# Server mechanics
daemon = False
pidfile = None
umask = 0
user = None
group = None
tmp_upload_dir = None

# SSL (disabled - use nginx reverse proxy)
keyfile = None
certfile = None

# Hooks
def on_starting(server):
    print("🚀 PDFWala V10.0 starting...")

def on_reload(server):
    print("🔄 PDFWala V10.0 reloading...")

def when_ready(server):
    print("✅ PDFWala V10.0 ready to accept connections")

def on_exit(server):
    print("🛑 PDFWala V10.0 shutting down")
