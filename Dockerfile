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
    wget \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Build unrar from source so the binary matches the target architecture.
# RARLAB only publishes x86_64 prebuilt binaries; compiling via Buildx+QEMU
# produces a native binary for both linux/amd64 and linux/arm64.
RUN wget -q https://www.rarlab.com/rar/unrarsrc-7.2.5.tar.gz -O /tmp/unrarsrc.tar.gz \
    && tar xzf /tmp/unrarsrc.tar.gz -C /tmp \
    && cd /tmp/unrar \
    && make -j"$(nproc)" \
    && install -m 755 unrar /usr/local/bin/unrar \
    && rm -rf /tmp/unrar /tmp/unrarsrc.tar.gz

# -----------------------
# Stage 2: Final
# -----------------------
FROM python:3.14-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PATH="/opt/venv/bin:$PATH"

# Install system deps + tini + gosu
# fonts-liberation and fonts-dejavu-core are NOT optional: wrapped.py loads them
# by absolute path (see wrapped.py get_font) to render the yearly Wrapped images.
# Dropping them silently degrades those images to PIL's bitmap default font.
RUN apt-get update && apt-get install -y --no-install-recommends \
      git \
      unar \
      p7zip-full \
      poppler-utils \
      tini \
      gosu \
      wget \
      gnupg \
      ca-certificates \
      fonts-liberation \
      fonts-dejavu-core \
      libc6 \
      libexpat1 \
      libfontconfig1 \
      libgcc1 \
      libglib2.0-0 \
      libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

# unrar binary built from RARLAB source in the builder stage — matches the
# target architecture automatically under Buildx multi-arch builds.
# Falls back to 7z/unar if unavailable — see helpers.py extract_rar_with_unar()
COPY --from=builder /usr/local/bin/unrar /usr/local/bin/unrar

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv

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
    UMASK=000 \
    FLASK_ENV=production \
    MONITOR=no \
    XDG_CACHE_HOME=/config/.cache

# Setup entrypoint
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Use tini as PID 1
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]

# Default command - Gunicorn production WSGI server
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:5577", "--timeout", "120", "app:app"]
