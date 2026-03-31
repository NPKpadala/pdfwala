# Use a slim Python image to keep the size manageable
FROM python:3.11-slim

# 1. Install Linux System Dependencies (The "Engines")
RUN apt-get update && apt-get install -y \
    gcc \
    libffi-dev \
    libjpeg-dev \
    zlib1g-dev \
    libpng-dev \
    curl \
    # REQUIRED: For OCR (pytesseract)
    tesseract-ocr \
    # REQUIRED: For PDF-to-Image (pdf2image)
    poppler-utils \
    # REQUIRED: For Word/Excel to PDF (LibreOffice Headless)
    libreoffice-writer \
    libreoffice-calc \
    && rm -rf /var/lib/apt/lists/*

# 2. Set working directory
WORKDIR /app

# 3. Install Python Dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4. Copy application code
COPY . .

# 5. Create necessary folders and set permissions
RUN mkdir -p /tmp/pdfwala/uploads /tmp/pdfwala/outputs static logs && \
    useradd -m -u 1001 pdfwala && \
    chown -R pdfwala:pdfwala /app /tmp/pdfwala

# 6. Switch to non-root user for security
USER pdfwala

# 7. Start the application
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "4", "--timeout", "120", "app:app"]
