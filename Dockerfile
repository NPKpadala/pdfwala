# Dockerfile — PDFWala Production
FROM python:3.11-slim

# ─── System dependencies ──────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice \
    libreoffice-calc \
    libreoffice-writer \
    libreoffice-impress \
    ghostscript \
    tesseract-ocr \
    tesseract-ocr-eng \
    wkhtmltopdf \
    curl \
    libgl1 \
    libglib2.0-0 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# ─── Working directory ────────────────────────────────────────────
WORKDIR /app

# ─── Python dependencies ──────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ─── Application files ────────────────────────────────────────────
COPY app.py .
COPY gunicorn.conf.py .

# ─── Directories ──────────────────────────────────────────────────
RUN mkdir -p /app/uploads /app/outputs /app/static

# ─── Non-root user ────────────────────────────────────────────────
RUN useradd -m -u 1001 pdfwala && \
    chown -R pdfwala:pdfwala /app
USER pdfwala

# ─── Start ────────────────────────────────────────────────────────
EXPOSE 5000
CMD ["gunicorn", "--config", "gunicorn.conf.py", "app:app"]
