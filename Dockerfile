FROM python:3.11-slim AS base

# Install system dependencies
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
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create data directories (consistent paths)
RUN mkdir -p /home/opc/pdfwala/uploads \
    /home/opc/pdfwala/outputs \
    /home/opc/pdfwala/temp

# Environment variables
ENV BASE_DIR=/app \
    BASE_DATA_DIR=/home/opc/pdfwala \
    UPLOAD_FOLDER=/home/opc/pdfwala/uploads \
    OUTPUT_FOLDER=/home/opc/pdfwala/outputs \
    TEMP_FOLDER=/home/opc/pdfwala/temp \
    LOG_LEVEL=INFO \
    PYTHONUNBUFFERED=1

EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/health || exit 1

# FIXED: Use gunicorn.conf.py instead of inline flags
# This resolves the conflict - all settings now come from the config file
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:application"]
