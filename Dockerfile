# Use official Python slim image
FROM python:3.11-slim

# install system deps (tesseract + fonts + build essentials)
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      tesseract-ocr \
      libtesseract-dev \
      libleptonica-dev \
      pkg-config \
      gcc \
      libjpeg-dev \
      zlib1g-dev \
      poppler-utils \
      fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Create app user (non-root)
RUN useradd --create-home --shell /bin/bash appuser
WORKDIR /home/appuser/app
COPY --chown=appuser:appuser . /home/appuser/app

# Install python deps
ENV PIP_NO_CACHE_DIR=1
RUN python -m pip install --upgrade pip
RUN pip install --no-cache-dir -r requirements.txt

# Ensure /data exists for Render disk mount
RUN mkdir -p /data && chown appuser:appuser /data

USER appuser

# Expose nothing (background worker). Start polling.
CMD ["python", "bot.py"]
