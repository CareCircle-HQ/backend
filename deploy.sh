#!/usr/bin/env bash
#
# One-shot deploy for the CareCircle backend (systemd + gunicorn + nginx).
#
# Usage (run from ~/backend on the server):
#   ./deploy.sh           # pull, install deps, migrate, collectstatic, restart
#   ./deploy.sh --seed    # ...and also load api/fixtures/reference_data.json
#
# --seed is OPTIONAL: loaddata upserts the reference tables (Agent,
# ProgramPipeline, AllowedZipCode, Service) by primary key, so running it again
# overwrites any manual edits made to those rows in the admin. Only pass it when
# you actually want to (re)seed reference data.
set -euo pipefail

# Always operate from the directory this script lives in.
cd "$(dirname "$0")"

SEED=false
for arg in "$@"; do
  case "$arg" in
    --seed) SEED=true ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

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

if [ "$SEED" = true ]; then
  echo "==> Seeding reference data (reference_data fixture)"
  python manage.py loaddata reference_data
fi

echo "==> Collecting static files"
python manage.py collectstatic --noinput

echo "==> Restarting services"
sudo systemctl daemon-reload
sudo systemctl restart gunicorn
sudo systemctl restart nginx

echo "==> Done. Quick health check:"
curl -fsS https://www.carecircleinternal.com/ && echo
