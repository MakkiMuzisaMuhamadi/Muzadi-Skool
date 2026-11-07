#!/usr/bin/env bash
# Upgrade ALL schools at once (they share one codebase and one database).
#
#   sudo -u school360 /srv/school360/app/deploy/scripts/deploy.sh          # deploy branch "main"
#   sudo -u school360 /srv/school360/app/deploy/scripts/deploy.sh v2.4     # deploy a tag
#
# Steps: backup -> maintenance page -> pull -> install -> migrate -> collectstatic
#        -> check -> restart -> maintenance off. Stops at the first error
#        (maintenance page stays on so nobody sees a half-upgraded system).
set -euo pipefail

APP=/srv/school360/app
VENV=/srv/school360/venv
REF="${1:-main}"
MAINT=/srv/school360/maintenance.on
PY="$VENV/bin/python"

cd "$APP"
echo "==> 1/8 Backup before upgrading"
"$APP/deploy/scripts/backup.sh" pre-deploy

echo "==> 2/8 Maintenance page ON"
touch "$MAINT"
trap 'echo "!! Deploy FAILED - maintenance page is still on. Fix, then run again, or: rm $MAINT"' ERR

echo "==> 3/8 Get code ($REF)"
git fetch --all --tags --prune
git checkout -q "$REF"
git pull -q --ff-only origin "$REF" 2>/dev/null || true
echo "    now at $(git log -1 --format='%h %s')"

echo "==> 4/8 Install requirements"
"$VENV/bin/pip" install -q -r requirements.txt

echo "==> 5/8 Database migrations"
"$PY" manage.py migrate --noinput

echo "==> 6/8 Static files"
"$PY" manage.py collectstatic --noinput -v 0

echo "==> 7/8 Django checks"
"$PY" manage.py check --deploy --fail-level ERROR

echo "==> 8/8 Restart app and workers"
sudo /bin/systemctl restart school360-gunicorn school360-celery school360-celery-reports school360-celerybeat

rm -f "$MAINT"
trap - ERR
echo "==> Done. Every school is on $(git log -1 --format='%h') . Post a release note at /platform/release-notes/"
