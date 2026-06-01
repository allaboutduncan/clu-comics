#!/usr/bin/env bash
set -euo pipefail

# Defaults set in Dockerfile (PUID=99, PGID=100) — can be overridden.
PUID="${PUID:-99}"
PGID="${PGID:-100}"
UMASK="${UMASK:-002}"

# Ensure the group exists (re-use if it already does)
if ! getent group "${PGID}" >/dev/null 2>&1; then
  groupadd -g "${PGID}" comics 2>/dev/null || true
else
  # If a group with PGID exists, capture its name; otherwise fall back
  EXISTING_GROUP="$(getent group "${PGID}" | cut -d: -f1 || true)"
  [ -n "${EXISTING_GROUP}" ] && GROUP_NAME="${EXISTING_GROUP}" || GROUP_NAME="comics"
fi
GROUP_NAME="${GROUP_NAME:-comics}"

# Ensure the user exists (re-use if it already does)
if ! id -u "${PUID}" >/dev/null 2>&1; then
  useradd -u "${PUID}" -g "${PGID}" -M -s /usr/sbin/nologin cluser 2>/dev/null || true
fi

# Make sure name resolves even if user existed already with a different name
if ! id cluser >/dev/null 2>&1; then
  # Create a name bound to the UID/GID if needed
  useradd -u "${PUID}" -g "${PGID}" -M -s /usr/sbin/nologin cluser 2>/dev/null || true
fi

# Apply umask for the session
umask "${UMASK}"

# Directories the app writes to (safe to chown lazily)
for d in /app/logs /app/static /config; do
  mkdir -p "$d"
  # Only fix ownership if needed to avoid slow recursive chown every start
  if [ -e "$d" ]; then
    # Ensure top dir is owned; skip recursive unless mismatched inside
    if [ "$(stat -c '%u:%g' "$d")" != "${PUID}:${PGID}" ]; then
      chown "${PUID}:${PGID}" "$d"
    fi
    # Fix nested items that are mismatched (fast when already correct)
    # Use -print0 and xargs -0 to handle filenames with spaces and special characters
    find "$d" \( ! -user "${PUID}" -o ! -group "${PGID}" \) -print0 2>/dev/null | xargs -0 -r chown "${PUID}:${PGID}"
  fi
done

# Handle mounted volumes - DON'T change ownership, just ensure they exist
# These are Windows volumes that can't have Unix ownership changed
# IMPORTANT: For Windows/WSL, you need to set PUID/PGID to match your Windows user
# To find your Windows user ID in WSL, run: id -u $USER

# Data directory (mounted volume)
if [ ! -e /data ]; then
  mkdir -p /data
fi

# Downloads directory (mounted volume) 
if [ ! -e /downloads ]; then
  mkdir -p /downloads
fi

# Create required subdirectories in downloads if they don't exist
# Don't change ownership - these inherit from the mounted volume
for subdir in temp processed; do
  if [ ! -e "/downloads/${subdir}" ]; then
    mkdir -p "/downloads/${subdir}"
  fi
done

# Ensure new sub-folders inherit the share's group (NAS-friendly, avoids needing 777).
# setgid is inherited by child dirs, so this propagates to future folders automatically.
CFG_TARGET="$(awk -F= '/^TARGET/ {print $2}' /config/config.ini 2>/dev/null | tr -d '\r')"
for p in /data /downloads "${CFG_TARGET}"; do
  [ -n "$p" ] || continue
  [ -d "$p" ] || continue
  chmod g+s "$p" 2>/dev/null || true   # non-recursive, fast; failures on Windows mounts are harmless
done

# Log the directory statuses for debugging
echo "Data directory status:"
ls -la /data 2>/dev/null || echo "  /data directory not accessible or empty"

echo "Downloads directory status:"
ls -la /downloads 2>/dev/null || echo "  /downloads directory not accessible or empty"

echo "Downloads subdirectories status:"
ls -la /downloads/temp 2>/dev/null || echo "  /downloads/temp directory not accessible or empty"
ls -la /downloads/processed 2>/dev/null || echo "  /downloads/processed directory not accessible or empty"

# Show actual ownership of mounted volumes (important for debugging)
echo "Mounted volume ownership (for PUID/PGID configuration):"
if [ -e /data ]; then
  data_owner=$(stat -c '%u:%g' /data 2>/dev/null || echo "unknown")
  echo "  /data owned by: ${data_owner}"
  # Check if PUID/PGID matches data ownership
  if [ "$data_owner" != "unknown" ] && [ "$data_owner" != "${PUID}:${PGID}" ]; then
    echo "  ⚠️  WARNING: /data ownership (${data_owner}) doesn't match PUID:PGID (${PUID}:${PGID})"
    echo "     This may cause permission issues. Consider setting PUID and PGID to match."
  fi
fi
if [ -e /downloads ]; then
  downloads_owner=$(stat -c '%u:%g' /downloads 2>/dev/null || echo "unknown")
  echo "  /downloads owned by: ${downloads_owner}"
  # Check if PUID/PGID matches downloads ownership
  if [ "$downloads_owner" != "unknown" ] && [ "$downloads_owner" != "${PUID}:${PGID}" ]; then
    echo "  ⚠️  WARNING: /downloads ownership (${downloads_owner}) doesn't match PUID:PGID (${PUID}:${PGID})"
    echo "     This may cause permission issues. Consider setting PUID and PGID to match."
  fi
fi

# Test write permissions
echo "Testing write permissions:"
if touch /downloads/temp/test_write 2>/dev/null; then
  echo "  ✓ Can write to /downloads/temp"
  rm -f /downloads/temp/test_write
else
  echo "  ✗ Cannot write to /downloads/temp"
fi

if touch /downloads/processed/test_write 2>/dev/null; then
  echo "  ✓ Can write to /downloads/processed"
  rm -f /downloads/processed/test_write
else
  echo "  ✗ Cannot write to /downloads/processed"
fi

# Additional Windows/WSL debugging
echo "Windows/WSL specific debugging:"
echo "  Current working directory: $(pwd)"
echo "  Current user: $(whoami)"
echo "  Current UID: $(id -u)"
echo "  Current GID: $(id -g)"
echo "  Downloads directory permissions:"
ls -la /downloads/ 2>/dev/null || echo "    Cannot list /downloads"
echo "  Downloads temp directory permissions:"
ls -la /downloads/temp/ 2>/dev/null || echo "    Cannot list /downloads/temp"

# Test creating a file with the exact pattern the app uses
echo "Testing app-specific file creation pattern:"
test_filename="test_file.cbr.0.crdownload"
test_path="/downloads/temp/${test_filename}"
if touch "${test_path}" 2>/dev/null; then
  echo "  ✓ Can create app-style temp file: ${test_filename}"
  rm -f "${test_path}"
else
  echo "  ✗ Cannot create app-style temp file: ${test_filename}"
  echo "    Error details: $(touch "${test_path}" 2>&1)"
fi

# Check mount information for Windows volumes
echo "Mount information for Windows volumes:"
if command -v mount >/dev/null 2>&1; then
  mount | grep -E "(data|downloads)" || echo "  No specific mount info found"
fi

# Check if this is a Windows filesystem
echo "Filesystem type information:"
if command -v stat >/dev/null 2>&1; then
  if [ -e /downloads ]; then
    echo "  /downloads filesystem: $(stat -f -c %T /downloads 2>/dev/null || echo 'unknown')"
  fi
  if [ -e /data ]; then
    echo "  /data filesystem: $(stat -f -c %T /data 2>/dev/null || echo 'unknown')"
  fi
fi

# Clear log files on restart (prevents timeout issues with large logs)
echo "Clearing old log files..."
for logfile in /app/logs/app.log /app/logs/monitor.log /config/logs/app.log /config/logs/monitor.log; do
  if [ -f "$logfile" ]; then
    > "$logfile"
    echo "  Cleared: $logfile"
  fi
done

# Show who we plan to run as (helps with Unraid troubleshooting)
echo "Starting as UID:GID ${PUID}:${PGID} (umask ${UMASK})"
echo "MONITOR=${MONITOR}"

# Decide who to run as based on writability of key paths
TARGET_USER="${PUID}:${PGID}"
RUN_AS_ROOT=0

# Helper to test write
can_write() { gosu "${TARGET_USER}" sh -c "touch \"$1\"/.writetest && rm -f \"$1\"/.writetest"; }

NEED_ROOT=0
for p in /downloads/temp /downloads/processed /data "$(awk -F= '/^TARGET/ {print $2}' /config/config.ini 2>/dev/null | tr -d '\r')" ; do
  [ -n "$p" ] || continue
  [ -d "$p" ] || continue
  if ! can_write "$p" 2>/dev/null ; then
    echo "⚠️  $p not writable by ${TARGET_USER}"
    NEED_ROOT=1
  fi
done

if [ "$NEED_ROOT" = "1" ]; then
  echo "Falling back to root due to non-writable mounts."
  exec "$@"
else
  exec gosu "${TARGET_USER}" "$@"
fi