# syntax = docker/dockerfile:1

# -----------------------
# Stage 1: Builder
# -----------------------
FROM python:3.14-slim-bookworm AS builder

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# -----------------------
# Stage 2: Final
# -----------------------
FROM python:3.14-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/venv/bin:$PATH"

# Install system deps + tini + gosu + Playwright dependencies
# These dependencies are based on the standard Playwright requirements for Debian
RUN apt-get update && apt-get install -y --no-install-recommends \
      git \
      unar \
      poppler-utils \
      tini \
      gosu \
      wget \
      gnupg \
      ca-certificates \
      fonts-liberation \
      fonts-dejavu-core \
      libasound2 \
      libatk-bridge2.0-0 \
      libatk1.0-0 \
      libc6 \
      libcairo2 \
      libcups2 \
      libdbus-1-3 \
      libexpat1 \
      libfontconfig1 \
      libgbm1 \
      libgcc1 \
      libglib2.0-0 \
      libgtk-3-0 \
      libnspr4 \
      libnss3 \
      libpango-1.0-0 \
      libpangocairo-1.0-0 \
      libstdc++6 \
      libx11-6 \
      libx11-xcb1 \
      libxcb1 \
      libxcomposite1 \
      libxcursor1 \
      libxdamage1 \
      libxext6 \
      libxfixes3 \
      libxi6 \
      libxrandr2 \
      libxrender1 \
      libxss1 \
      libxtst6 \
      lsb-release \
      xdg-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

# Install Playwright browsers (chromium only for scraping)
# We run this here to ensure browsers are installed in the final image
RUN playwright install chromium

# Copy application source
COPY . .

# Create runtime dirs
RUN mkdir -p /app/logs /app/static /config /data /downloads/temp /downloads/processed

# Ensure /app/templates is readable by all users
RUN chmod -R 755 /app/templates

# Expose Flask port
EXPOSE 5577

# Set default env vars
ENV PUID=99 \
    PGID=100 \
    UMASK=022 \
    FLASK_ENV=production \
    MONITOR=no

# Setup entrypoint
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Use tini as PID 1
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]

# Default command - Gunicorn production WSGI server
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:5577", "--timeout", "120", "app:app"]
