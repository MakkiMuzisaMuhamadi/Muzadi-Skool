#!/usr/bin/env bash
# Restore a backup. Practise this once a month on a TEST database:
#
#   restore.sh /srv/school360/backups/daily/db_zimak_2026-11-01_0215.dump  zimak_restore_test
#
# Restoring over the live database (second argument = the live DB name) replaces
# ALL schools' data - only do that after stopping the app.
set -euo pipefail
DUMP="$1"
TARGET_DB="${2:?give the database name to restore into}"
APP=/srv/school360/app
set -a; . "$APP/.env"; set +a
export PGPASSWORD="$DB_PASSWORD"

createdb -h "${DB_HOST:-localhost}" -U "$DB_USER" "$TARGET_DB" 2>/dev/null || true
pg_restore -h "${DB_HOST:-localhost}" -U "$DB_USER" -d "$TARGET_DB" --clean --if-exists --no-owner "$DUMP"
psql -h "${DB_HOST:-localhost}" -U "$DB_USER" -d "$TARGET_DB" -tc \
  'select count(*) as schools from "mainApp_school";' | xargs echo "schools in restored DB:"
