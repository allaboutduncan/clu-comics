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

# FlareSolverr, bundled so Cloudflare-challenged GetComics mirrors can be
# passed without users running a second container (core/flaresolverr.py).
# Pinned: a FlareSolverr bump is a deliberate change, tested against the
# Chromium that bookworm ships. It gets its OWN venv so selenium and its pins
# can never collide with CLU's requirements.
ARG FLARESOLVERR_VERSION=3.5.2
RUN wget -q "https://github.com/FlareSolverr/FlareSolverr/archive/refs/tags/v${FLARESOLVERR_VERSION}.tar.gz" \
        -O /tmp/flaresolverr.tar.gz \
    && mkdir -p /opt/flaresolverr \
    && tar xzf /tmp/flaresolverr.tar.gz -C /opt/flaresolverr --strip-components=1 \
    && rm -f /tmp/flaresolverr.tar.gz \
    && find /opt/flaresolverr -mindepth 1 -maxdepth 1 ! -name src ! -name package.json ! -name requirements.txt -exec rm -rf {} + \
    && rm -f /opt/flaresolverr/src/tests.py /opt/flaresolverr/src/tests_sites.py \
    && python -m venv /opt/flaresolverr-venv \
    && /opt/flaresolverr-venv/bin/pip install --no-cache-dir -r /opt/flaresolverr/requirements.txt

# Empty stand-ins for two packages Chromium pulls in but never needs headless
# under Xvfb: the Mesa DRI drivers and the Adwaita icon theme. Same trick as
# the official FlareSolverr image; together they are well over 100 MB.
RUN apt-get update && apt-get install -y --no-install-recommends equivs \
    && for pkg in libgl1-mesa-dri adwaita-icon-theme; do \
         printf 'Section: misc\nPriority: optional\nStandards-Version: 3.9.2\nPackage: %s\nVersion: 99.0.0\nDescription: Dummy package for %s\n' "$pkg" "$pkg" > "/tmp/$pkg.ctl" \
         && (cd /tmp && equivs-build "$pkg.ctl") \
         && mv /tmp/${pkg}_*.deb "/opt/$pkg.deb"; \
       done \
    && rm -rf /var/lib/apt/lists/*

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
#
# sqlite3 is here for the salvage path only. tools/repair_db.py prefers the
# CLI's ".recover" and falls back to a lossier pure-Python per-table salvage;
# without the package cli_has_recover() is always False, so the better path was
# dead code in production. ~1.5 MB for a materially better recovery of a
# damaged library.
#
# chromium, chromium-driver, xvfb and xauth are FlareSolverr's browser (see the
# builder stage). It is started only when a download meets a Cloudflare
# challenge and stopped when idle -- see core/flaresolverr_supervisor.py. The
# two dummy .debs are installed first so apt treats their real packages as
# satisfied.
COPY --from=builder /opt/libgl1-mesa-dri.deb /opt/adwaita-icon-theme.deb /tmp/
RUN dpkg -i /tmp/libgl1-mesa-dri.deb /tmp/adwaita-icon-theme.deb \
    && rm -f /tmp/libgl1-mesa-dri.deb /tmp/adwaita-icon-theme.deb \
    && apt-get update && apt-get install -y --no-install-recommends \
      chromium \
      chromium-driver \
      xvfb \
      xauth \
      git \
      unar \
      p7zip-full \
      poppler-utils \
      sqlite3 \
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
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /usr/lib/*-linux-gnu/libmfxhw* /usr/lib/*-linux-gnu/mfx/*

# unrar binary built from RARLAB source in the builder stage — matches the
# target architecture automatically under Buildx multi-arch builds.
# Falls back to 7z/unar if unavailable — see helpers.py extract_rar_with_unar()
COPY --from=builder /usr/local/bin/unrar /usr/local/bin/unrar

WORKDIR /app

# Copy virtual environment from builder
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/flaresolverr /opt/flaresolverr
COPY --from=builder /opt/flaresolverr-venv /opt/flaresolverr-venv

# Copy application source
COPY . .

# Create runtime dirs
RUN mkdir -p /app/logs /app/static /config /cache /data /downloads

# FlareSolverr skips its runtime chromedriver download only when it finds a
# driver at exactly /app/chromedriver (hard-coded in its utils.py). It then
# patches that binary in place on first use -- which fails when CLU runs as
# PUID and the file is root's. So copy the driver that matches the apt Chromium
# and patch it here, as root; at runtime it is already patched and never
# written.
RUN cp /usr/bin/chromedriver /app/chromedriver \
    && chmod 755 /app/chromedriver \
    && HOME=/tmp/fs-build /opt/flaresolverr-venv/bin/python -c "import sys; sys.path.insert(0, '/opt/flaresolverr/src'); from undetected_chromedriver.patcher import Patcher; p = Patcher(executable_path='/app/chromedriver'); p.patch_exe(); assert p.is_binary_patched(), 'chromedriver patch failed'" \
    && rm -rf /tmp/fs-build

# Ensure /app/templates is readable by all users
RUN chmod -R 755 /app/templates

# Expose Flask port
EXPOSE 5577

# Set default env vars
ENV PUID=99 \
    PGID=100 \
    UMASK=000 \
    FLASK_ENV=production \
    XDG_CACHE_HOME=/config/.cache

# Setup entrypoint
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

# Use tini as PID 1
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]

# Default command - Gunicorn production WSGI server
CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:5577", "--timeout", "120", "app:app"]
