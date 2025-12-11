# Dockerfile
FROM python:3.11-slim

# install system deps (tesseract + fonts if OCR needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr \
    libtesseract-dev \
    poppler-utils \
    pkg-config \
    gcc \
    libjpeg-dev \
    zlib1g-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

# Make /data usable by non-root (Render uses 'appuser' in their starter; keep simple)
RUN mkdir -p /data && chown -R 1000:1000 /data || true

ENV DATA_DIR=/data
EXPOSE 8000

# start uvicorn (the python script will also start the bot in a background thread)
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
