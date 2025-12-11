# Dockerfile
FROM python:3.11-slim

# Install Tesseract + build deps (only if you use OCR)
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

# copy requirements first for better caching
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# copy app
COPY . /app

# make /data available
RUN mkdir -p /data && chown -R 1000:1000 /data || true
ENV DATA_DIR=/data

EXPOSE 8000

# start the FastAPI webhook (uvicorn). bot.py will be started by app.py on startup (if present).
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
