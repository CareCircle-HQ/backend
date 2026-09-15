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

echo "==> Reloading services"
sudo systemctl daemon-reload
# GRACEFUL, not restart. `systemctl restart gunicorn` deletes the unix socket, so
# for the second or two the process is down nginx answers every request with 502
# -- "connect() to unix:.../gunicorn.sock failed (2: No such file or directory)".
# That put 25 5xx on the ALB on 2026-09-15 and tripped the alarm: every deploy was
# a small outage. SIGHUP (ExecReload in deploy/gunicorn.service) starts new
# workers, loads the new code, retires the old ones, and keeps the socket.
#
# Falls back to restart when the unit has no ExecReload yet, so this script still
# works on a host whose gunicorn.service predates deploy/gunicorn.service.
if ! sudo systemctl reload gunicorn; then
  echo "==> reload failed (unit missing ExecReload?) -- falling back to restart"
  echo "    install deploy/gunicorn.service to make deploys graceful"
  sudo systemctl restart gunicorn
fi
# nginx reload is zero-downtime by design; restart drops in-flight connections.
sudo systemctl reload nginx
# Celery worker (async CSV imports). Only restart if the unit is installed, so
# this script still works on hosts that haven't set up the worker yet.
if systemctl list-unit-files | grep -q '^celery-worker\.service'; then
  # NOTE: this is a hard restart, and SIGTERM gives Celery a warm shutdown that
  # waits for running tasks -- but systemd SIGKILLs at TimeoutStopSec (90s by
  # default). A long job therefore dies mid-run: that is exactly what produced
  # "Worker restarted mid-run; marked failed to unblock" on ImportRun #1449
  # (member_prep, 8000/15148 rows). The stale-run takeover in
  # PurchaseOrderMemberPrepView now recovers from it automatically, but a deploy
  # during a long import still loses that run's progress.
  echo "==> Restarting celery-worker"
  sudo systemctl restart celery-worker
fi

echo "==> Done. Quick health check:"
curl -fsS https://www.carecircleinternal.com/ && echo
