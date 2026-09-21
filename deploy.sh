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
# RETRIED, because `systemctl reload gunicorn` returns once systemd has DELIVERED
# the HUP -- not once the new workers are answering. Re-importing this Django app
# takes a second or two, and the old workers drain in parallel, so a single curl
# fired on the next line can legitimately hit a moment where the socket answers
# nothing. That printed a bare "502" at the end of an otherwise perfect deploy,
# which is worse than no check: it teaches you to ignore the one that matters.
#
# Prints the status code rather than the homepage HTML, which the previous -fsS
# dumped to the terminal on success.
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  code=$(curl -o /dev/null -s -w '%{http_code}' https://www.carecircleinternal.com/ || echo 000)
  if [ "$code" = "200" ]; then
    echo "    HTTP $code (attempt $attempt)"
    break
  fi
  if [ "$attempt" = "10" ]; then
    echo "    STILL FAILING after 10 attempts: HTTP $code"
    echo "    sudo systemctl status gunicorn --no-pager -l | tail -20"
    echo "    sudo journalctl -u gunicorn -n 60 --no-pager"
    exit 1
  fi
  sleep 2
done
