#!/usr/bin/env bash
#
# One-shot deploy for the CareCircle backend (systemd + gunicorn + nginx).
#
# Usage (run from ~/backend on the server):
#   ./deploy.sh           # pull, install deps, migrate, collectstatic, restart
#
# NOTE: reference data (api/fixtures/reference_data.json) is intentionally NOT
# loaded here. Seed it manually one time:
#   python manage.py loaddata reference_data
set -euo pipefail

# Always operate from the directory this script lives in.
cd "$(dirname "$0")"

echo "==> Pulling latest code (main)"
git checkout main
git pull --ff-only origin main

# Activate the virtualenv (server uses ./venv; fall back to ./.venv).
if [ -d venv ]; then
  # shellcheck disable=SC1091
  source venv/bin/activate
elif [ -d .venv ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
else
  echo "ERROR: no venv/ or .venv/ found" >&2
  exit 1
fi

echo "==> Installing dependencies"
pip install --upgrade pip
pip install -r requirements.txt

echo "==> Applying database migrations"
python manage.py migrate --noinput

echo "==> Collecting static files"
python manage.py collectstatic --noinput

echo "==> Restarting services"
sudo systemctl daemon-reload
sudo systemctl restart gunicorn
sudo systemctl restart nginx
# Celery worker (async CSV imports). Only restart if the unit is installed, so
# this script still works on hosts that haven't set up the worker yet.
if systemctl list-unit-files | grep -q '^celery-worker\.service'; then
  echo "==> Restarting celery-worker"
  sudo systemctl restart celery-worker
fi

echo "==> Done. Quick health check:"
curl -fsS https://www.carecircleinternal.com/ && echo
