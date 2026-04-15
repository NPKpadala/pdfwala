import multiprocessing

bind = "0.0.0.0:5000"

workers = 4
worker_class = "gthread"
threads = 8

timeout = 600
graceful_timeout = 120
keepalive = 5

limit_request_line = 8190
limit_request_fields = 200

max_requests = 1000
max_requests_jitter = 30

accesslog = "-"
errorlog = "/app/gunicorn_error.log"
loglevel = "info"
access_log_format = '%(h)s "%(r)s" %(s)s %(b)s %(D)sµs'

proc_name = "pdfwala"

preload_app = False

worker_tmp_dir = "/dev/shm"
