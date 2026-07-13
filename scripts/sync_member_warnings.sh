#!/usr/bin/env bash
#
# Cron wrapper for the nightly member/household warning snapshot refresh.
#
# Runs `manage.py sync_member_warnings` with the project's virtualenv and
# appends a timestamped record of each run to backend/logs/sync_member_warnings.log.
#
# The warning snapshot is also kept current on the fly (a live scan when a
# profile is opened, plus a sync on extension case saves / CSV imports); this
# nightly sweep is the safety net for TIME-BASED checks (e.g. an insurance or
# internal-service authorization that simply lapses with the passing of a day).
#
# Any extra args are passed through, e.g.:
#   scripts/sync_member_warnings.sh --limit 100
#
# Install (run daily at 03:00, after the 02:00 daily_pull) with the ABSOLUTE
# path to this script on the target machine, e.g. on the server:
#   crontab -e
#   0 3 * * * /home/ubuntu/backend/scripts/sync_member_warnings.sh
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
LOG_FILE="$LOG_DIR/sync_member_warnings.log"

mkdir -p "$LOG_DIR"

{
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %z') sync_member_warnings start ====="
  "$PYTHON" "$BACKEND_DIR/manage.py" sync_member_warnings "$@"
  status=$?
  echo "===== $(date '+%Y-%m-%d %H:%M:%S %z') sync_member_warnings end (exit ${status}) ====="
  echo
} >> "$LOG_FILE" 2>&1
