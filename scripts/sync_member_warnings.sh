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
# Install (run daily at 03:00, after the 02:00 daily_pull):
#   crontab -e
#   0 3 * * * /Users/alex/Projects/ext/backend/scripts/sync_member_warnings.sh
#
set -uo pipefail

BACKEND_DIR="/Users/alex/Projects/ext/backend"
PYTHON="$BACKEND_DIR/.venv/bin/python"
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
