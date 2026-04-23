# PDFWala Enterprise V11.0.0
# Multi-stage build with non-root user for production security

FROM python:3.11-slim-bookworm AS builder

# Install system dependencies for Python packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libffi-dev \
    libjpeg-dev \
    zlib1g-dev \
    libssl-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies in a virtual environment
COPY requirements.txt .
RUN python -m venv /opt/venv && \
    . /opt/venv/bin/activate && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir gunicorn

# ----------------------------------------------------------------
# FINAL STAGE
# ----------------------------------------------------------------
FROM python:3.11-slim-bookworm

# Install runtime dependencies (LibreOffice, Ghostscript, Tesseract, wkhtmltopdf)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-core \
    libreoffice-writer \
    libreoffice-calc \
    libreoffice-impress \
    ghostscript \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-spa \
    tesseract-ocr-fra \
    wkhtmltopdf \
    curl \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Set working directory
WORKDIR /app

# Copy application code
COPY . .

# Create required directories with proper permissions
RUN mkdir -p /home/opc/pdfwala/uploads \
             /home/opc/pdfwala/outputs \
             /home/opc/pdfwala/temp \
             /home/opc/pdfwala/static

# Create non-root user for security (V11.0.0)
RUN groupadd -r pdfwala && \
    useradd -r -g pdfwala -s /bin/false pdfwala && \
    chown -R pdfwala:pdfwala /app && \
    chown -R pdfwala:pdfwala /home/opc/pdfwala

# Switch to non-root user
USER pdfwala

# Expose port
EXPOSE 5000

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:5000/api/v1/ready || exit 1

# Start Gunicorn
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
