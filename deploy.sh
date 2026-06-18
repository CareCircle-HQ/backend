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

echo "==> Pulling latest code"
git pull --ff-only

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

echo "==> Done. Quick health check:"
curl -fsS https://www.carecircleinternal.com/ && echo
