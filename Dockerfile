# Use official Python slim image
FROM python:3.11-slim

# install system deps (tesseract + fonts + build essentials)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      tesseract-ocr \
      libtesseract-dev \
      gcc \
      libjpeg-dev \
      libpng-dev \
      libwebp-dev \
      zlib1g-dev \
      libtiff5 \
      fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user and persistent data directory
RUN useradd -m appuser
RUN mkdir -p /data && chown appuser:appuser /data

WORKDIR /opt/app

COPY requirements.txt /opt/app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /opt/app
RUN chown -R appuser:appuser /opt/app

USER appuser
ENV DATA_DIR=/data

CMD ["python", "bot.py"]

USER appuser

# Expose nothing (background worker). Start polling.
CMD ["python", "bot.py"]
