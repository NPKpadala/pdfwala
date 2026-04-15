FROM python:3.11-slim AS base

# Install system dependencies
# NOTE: wkhtmltopdf removed - not available in Debian Trixie
# HTML to PDF will fallback to weasyprint
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice \
    ghostscript \
    tesseract-ocr \
    tesseract-ocr-eng \
    pngquant \
    fonts-dejavu \
    fonts-noto \
    fonts-liberation \
    libgl1 \
    curl \
    wget \
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

# Use gunicorn.conf.py
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:application"]
