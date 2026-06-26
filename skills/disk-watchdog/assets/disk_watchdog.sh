#!/usr/bin/env bash
# no_agent cron — alerts only when /data usage crosses the threshold.
#
# Empty stdout = silent: Hermes delivers nothing when there's nothing to say, so a
# healthy disk stays quiet. This is the classic watchdog pattern (zero LLM cost).
# Override the threshold with DISK_ALERT_THRESHOLD (percent) in the Hermes .env.
set -euo pipefail

threshold="${DISK_ALERT_THRESHOLD:-85}"

# /data is the Railway persistent volume (config, sessions, memories, cron output).
usage="$(df -P /data 2>/dev/null | awk 'NR==2 { gsub(/%/, "", $5); print $5 }')"

if [ -n "${usage:-}" ] && [ "$usage" -ge "$threshold" ]; then
  printf '⚠️ /data disk usage at %s%% (threshold %s%%)\n' "$usage" "$threshold"
fi
