#!/usr/bin/env bash
#
# Cron wrapper for the daily Unite Us pull.
#
# Runs `manage.py daily_pull` with the project's virtualenv and appends a
# timestamped record of each run to backend/logs/daily_pull.log. The command
# itself is a no-op unless UNITEUS_ENABLED=True (see api/.../daily_pull.py),
# so this is safe to schedule even before the integration is fully live.
#
# Any extra args are passed through, e.g.:
#   scripts/daily_pull.sh --client-limit 5
#   scripts/daily_pull.sh --force            # ignore UNITEUS_ENABLED
#
# Install (run daily at 02:00) with the ABSOLUTE path to this script on the
# target machine, e.g. on the server:
#   crontab -e
#   0 2 * * * /home/ubuntu/backend/scripts/daily_pull.sh
#
set -uo pipefail

# Resolve the backend dir from THIS script's own location so the same script
# works locally and on the server without editing a hardcoded path.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Pick the interpreter: an explicit $PYTHON_BIN override, else the project venv
# (named ".venv" locally, "venv" on the server), else system python3.
if [ -n "${PYTHON_BIN:-}" ]; then
  PYTHON="$PYTHON_BIN"
elif [ -x "$BACKEND_DIR/.venv/bin/python" ]; then
  PYTHON="$BACKEND_DIR/.venv/bin/python"
elif [ -x "$BACKEND_DIR/venv/bin/python" ]; then
  PYTHON="$BACKEND_DIR/venv/bin/python"
else
  PYTHON="python3"
fi
LOG_DIR="$BACKEND_DIR/logs"
LOG_FILE="$LOG_DIR/daily_pull.log"

mkdir -p "$LOG_DIR"

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %z') daily_pull start ====="
  "$PYTHON" "$BACKEND_DIR/manage.py" daily_pull --triggered-by cron "$@"
  status=$?
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %z') daily_pull end (exit ${status}) ====="
  echo
} >> "$LOG_FILE" 2>&1
