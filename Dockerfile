FROM ubuntu:22.04

# Set non-interactive mode to avoid timezone prompt
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies
RUN apt-get update && apt-get install -y \
    python3.11 python3.11-dev python3-pip \
    libreoffice \
    tesseract-ocr tesseract-ocr-eng \
    ghostscript poppler-utils \
    wkhtmltopdf \
    pngquant \
    default-jre-headless \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements first (better caching)
COPY requirements.txt .

# Install Python packages
RUN pip3 install --no-cache-dir -r requirements.txt

# Copy all application files
COPY app.py .
COPY gunicorn.conf.py .
COPY static/ ./static/

# Create necessary directories
RUN mkdir -p /app/uploads /app/outputs /app/cache

# Expose port
EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:5000/api/health || exit 1

# Run with gunicorn
CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
