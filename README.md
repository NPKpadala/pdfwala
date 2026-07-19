# PDFWala

Production PDF-processing platform, built and operated end to end — from the Flask app to the Nginx config it runs behind.

![Python](https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=white)
![Flask](https://img.shields.io/badge/Flask-000000?logo=flask&logoColor=white)
![Celery](https://img.shields.io/badge/Celery-37814A?logo=celery&logoColor=white)
![Redis](https://img.shields.io/badge/Redis-DC382D?logo=redis&logoColor=white)
![Docker](https://img.shields.io/badge/Docker-2496ED?logo=docker&logoColor=white)

## Architecture

```
Client ──> Nginx ──> Gunicorn / Flask ──> Redis queue ──> Celery workers
                                                            │
                                                PDF engines (PyMuPDF, pikepdf,
                                                pdfplumber, pdf2docx, reportlab)
```

- **Pipeline architecture** — heavy PDF jobs (convert, merge, flatten, extract) run asynchronously on Celery workers behind a Redis queue, keeping the web tier responsive.
- **Multiple PDF engines** — PyMuPDF, pikepdf, PyPDF2, pdfplumber, pdf2docx and reportlab, selected per operation.
- **Operations built in** — heartbeat monitoring, metrics output, deploy script, and a CVE check script; benchmarked under `benchmark/`.
- **Fully containerized** — `Dockerfile` + `docker-compose.yml` bring up the whole stack.

## Repository layout

| Path | Purpose |
|---|---|
| `app/` | Flask application (routes, controllers) |
| `engines/`, `workers/`, `tasks/` | PDF engines and async Celery jobs |
| `services/`, `core/`, `utils/` | Business logic and shared helpers |
| `nginx/`, `gunicorn.conf.py`, `deploy.sh` | Production serving and deployment |
| `tests/`, `benchmark/` | Test-suite and performance benchmarks |

## Run it

```bash
docker compose up --build
```

---

Part of my portfolio — more at [npkpadala.com](https://npkpadala.com).
