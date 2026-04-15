FROM python:3.11-slim AS base

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice \
    ghostscript \
    tesseract-ocr \
    tesseract-ocr-eng \
    wkhtmltopdf \
    pngquant \
    fonts-dejavu \
    fonts-noto \
    fonts-liberation \
    libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data/uploads /data/outputs /data/temp

ENV BASE_DIR=/app \
    BASE_DATA_DIR=/data \
    UPLOAD_FOLDER=/data/uploads \
    OUTPUT_FOLDER=/data/outputs \
    TEMP_FOLDER=/data/temp \
    LOG_LEVEL=INFO

EXPOSE 5000

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", \
     "--timeout", "300", "--keep-alive", "5", \
     "--worker-class", "gthread", "--threads", "2", \
     "app:application"]
