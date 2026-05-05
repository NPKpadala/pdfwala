FROM python:3.11-slim AS base

# ============================================================================
# Install system dependencies
# ============================================================================
RUN apt-get update && apt-get install -y --no-install-recommends \
    # PDF/Office tools
    libreoffice \
    ghostscript \
    tesseract-ocr \
    tesseract-ocr-eng \
    pngquant \
    poppler-utils \
    # Fonts for rendering
    fonts-dejavu \
    fonts-noto \
    fonts-liberation \
    # System libraries
    libgl1 \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    shared-mime-info \
    # Utilities
    curl \
    wget \
    # Clean up apt cache
    && rm -rf /var/lib/apt/lists/*

# ============================================================================
# 🔒 SECURITY: Create non-root user BEFORE copying application
# ============================================================================
RUN useradd -u 1000 -m -s /bin/bash pdfwala

WORKDIR /app

# ============================================================================
# Install Python dependencies (still as root — pip installs system-wide)
# ============================================================================
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ============================================================================
# Copy application code
# ============================================================================
COPY . .

# ============================================================================
# Create data directories with CORRECT ownership
# ============================================================================
RUN mkdir -p /home/pdfwala/data/uploads \
    /home/pdfwala/data/outputs \
    /home/pdfwala/data/temp && \
    chown -R pdfwala:pdfwala /home/pdfwala /app

# ============================================================================
# Environment variables (updated paths to match non-root user)
# ============================================================================
ENV BASE_DIR=/app \
    BASE_DATA_DIR=/home/pdfwala/data \
    UPLOAD_FOLDER=/home/pdfwala/data/uploads \
    OUTPUT_FOLDER=/home/pdfwala/data/outputs \
    TEMP_FOLDER=/home/pdfwala/data/temp \
    LOG_LEVEL=INFO \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# ============================================================================
# LibreOffice needs a writable home directory
# ============================================================================
RUN mkdir -p /home/pdfwala/.config && \
    chown -R pdfwala:pdfwala /home/pdfwala/.config

# ============================================================================
# 🔒 Switch to non-root user
# ============================================================================
USER pdfwala

EXPOSE 5000

# ============================================================================
# Health check (runs as pdfwala user now)
# ============================================================================
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:5000/health || exit 1

# ============================================================================
# Start application
# ============================================================================
CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:application"]
