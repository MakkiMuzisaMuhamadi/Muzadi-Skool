#!/usr/bin/env bash
# Nightly backup: database (pg_dump custom format) + uploaded files.
# Keeps 14 daily copies locally and copies them OFF the server with rclone
# (e.g. Backblaze B2, Google Drive, another VPS). A backup that only lives on the
# same server is lost together with the server.
#
#   backup.sh            -> /srv/school360/backups/daily/...
#   backup.sh pre-deploy -> /srv/school360/backups/pre-deploy/...
set -euo pipefail

KIND="${1:-daily}"
APP=/srv/school360/app
DEST_ROOT=/srv/school360/backups
DEST="$DEST_ROOT/$KIND"
KEEP_DAYS="${KEEP_DAYS:-14}"
STAMP=$(date +%Y-%m-%d_%H%M)
mkdir -p "$DEST"

# DB settings come from the app's .env
set -a; . "$APP/.env"; set +a
export PGPASSWORD="$DB_PASSWORD"

DB_FILE="$DEST/db_${DB_NAME}_${STAMP}.dump"
MEDIA_FILE="$DEST/media_${STAMP}.tar.gz"

pg_dump -h "${DB_HOST:-localhost}" -p "${DB_PORT:-5432}" -U "$DB_USER" -d "$DB_NAME" -Fc -f "$DB_FILE"
tar -czf "$MEDIA_FILE" -C "$APP" media

# quick integrity check of the dump
pg_restore --list "$DB_FILE" > /dev/null

find "$DEST" -type f -mtime +"$KEEP_DAYS" -delete

if command -v rclone >/dev/null && [ -n "${BACKUP_REMOTE:-}" ]; then
    rclone copy "$DEST" "$BACKUP_REMOTE/$KIND" --max-age 48h
    echo "copied to $BACKUP_REMOTE/$KIND"
else
    echo "WARNING: BACKUP_REMOTE not set in .env - backup is only on this server" >&2
fi
echo "backup ok: $DB_FILE ($(du -h "$DB_FILE" | cut -f1)), $MEDIA_FILE ($(du -h "$MEDIA_FILE" | cut -f1))"
