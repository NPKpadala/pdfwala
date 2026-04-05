FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV BASE_DIR=/home/opc/pdfwala

# System dependencies
RUN apt-get update && apt-get install -y \
    python3.11 python3.11-dev python3-pip \
    libreoffice \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-spa tesseract-ocr-fra \
    ghostscript \
    poppler-utils \
    default-jre-headless \
    wkhtmltopdf \
    libgl1-mesa-glx \
    libglib2.0-0 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static/ ./static/

RUN mkdir -p /home/opc/pdfwala/uploads /home/opc/pdfwala/outputs /home/opc/pdfwala/static

EXPOSE 5000

CMD ["gunicorn", "--config", "gunicorn.conf.py", "app:app"]
