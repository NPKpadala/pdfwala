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
    tesseract-ocr-hin \
    tesseract-ocr-tam \
    tesseract-ocr-tel \
    pngquant \
    poppler-utils \
    # FIX V14: qpdf needed for linearize_pdf (was missing — engine detects at runtime
    # but falls back to slower Ghostscript path without it)
    qpdf \
    # Fonts for rendering
    fonts-dejavu \
    fonts-noto \
    fonts-liberation \
    fonts-freefont-ttf \
    # System libraries for Pillow / image ops
    libgl1 \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libcairo2 \
    libgdk-pixbuf-2.0-0 \
    libffi-dev \
    # FIX V14: libjpeg-turbo for faster JPEG encode/decode in Pillow
    libjpeg62-turbo \
    libpng16-16 \
    libwebp7 \
    # FIX V14: libmagic for robust file type detection
    libmagic1 \
    shared-mime-info \
    # Utilities
    curl \
    wget \
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
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

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
# Environment variables
# ============================================================================
ENV BASE_DIR=/app \
    BASE_DATA_DIR=/home/pdfwala/data \
    UPLOAD_FOLDER=/home/pdfwala/data/uploads \
    OUTPUT_FOLDER=/home/pdfwala/data/outputs \
    TEMP_FOLDER=/home/pdfwala/data/temp \
    LOG_LEVEL=INFO \
    PYTHONUNBUFFERED=1 \
    # FIX V14.1: Always set PYTHONPATH so 'tasks', 'workers', 'core' etc.
    # are importable regardless of what CWD the process starts with
    PYTHONPATH=/app \
    # FIX V14: Prevent LibreOffice from trying to create a display connection
    DISPLAY="" \
    # FIX V14: Disable LibreOffice crash reporter (hangs headless)
    SAL_USE_VCLPLUGIN=svp

# ============================================================================
# LibreOffice needs a writable home directory
# ============================================================================
RUN mkdir -p /home/pdfwala/.config/libreoffice && \
    chown -R pdfwala:pdfwala /home/pdfwala/.config

# ============================================================================
# 🔒 Switch to non-root user
# ============================================================================
USER pdfwala

EXPOSE 5000

# ============================================================================
# Health check
# ============================================================================
HEALTHCHECK --interval=30s --timeout=15s --start-period=60s --retries=5 \
    CMD curl -f http://localhost:5000/health || exit 1

# ============================================================================
# Start application
# ============================================================================
CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:application"]
