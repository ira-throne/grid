#!/usr/bin/env bash
# Runs one job with the project's Python, appending output to logs/<job>.log.
# flock skips the run if the previous one for the same job (and arguments)
# is still going.
# Usage: scripts/run.sh <job> [extra args]
#   ingest jobs: caiso | caiso_hourly | caiso_outlook | curtailment | weather | weather_forecast
#                (runs ingest/<job>_ingest.py)
#   forecast | train  (runs python -m forecast.run / forecast.train)
set -euo pipefail
cd "$(dirname "$0")/.."
JOB="$1"; shift
mkdir -p logs
# Lock name includes the arguments, so "caiso" and "caiso --market DAM" don't block each other
LOCK="/tmp/gridpulse-${JOB}$(printf '%s' "$*" | tr -c 'a-zA-Z0-9' '_').lock"
case "$JOB" in
    forecast) CMD=(.venv/bin/python -m forecast.run "$@") ;;
    train)    CMD=(.venv/bin/python -m forecast.train "$@") ;;
    *)        CMD=(.venv/bin/python "ingest/${JOB}_ingest.py" "$@") ;;
esac
{ echo "== $(date '+%F %T') ${CMD[*]}"; } >> "logs/${JOB}.log"
flock -n "$LOCK" "${CMD[@]}" >> "logs/${JOB}.log" 2>&1
