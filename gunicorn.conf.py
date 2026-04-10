import multiprocessing

bind = "0.0.0.0:5000"

# 2 workers is enough for this server
workers = 2
worker_class = "gthread"
threads = 4

timeout = 600
graceful_timeout = 120
keepalive = 5

limit_request_line = 8190
limit_request_fields = 200

# Restart workers to prevent memory bloat
max_requests = 1000
max_requests_jitter = 30

accesslog = "-"
errorlog = "-"
loglevel = "info"
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)sµs'

proc_name = "pdfwala"

# DISABLED - causes crashes with PyMuPDF/fitz
preload_app = False

worker_tmp_dir = "/dev/shm"
