---
name: disk-watchdog
description: Check the /data volume's disk usage and, if asked, install a recurring cron alert. Use when the user asks to monitor disk space, set up a disk/storage alert, or wants to know how full the volume is. Ships a zero-LLM-cost watchdog script the agent installs on request.
version: 1.0.0
license: MIT
metadata:
  hermes:
    tags: [ops, monitoring, cron]
---

# Disk Watchdog

Monitor the Railway `/data` volume (where Hermes keeps config, sessions, memories,
and cron output). Two modes:

## 1. One-off check (do this directly)
Run the bundled script once and report the result:
```
bash {skill_dir}/assets/disk_watchdog.sh
```
It prints a line only when usage is at/above the threshold (default 85%); empty
output means the disk is healthy — say so.

## 2. Recurring alert (only when the user asks to "set up" / "schedule" it)
This installs a `no_agent` cron — it runs the script on a schedule with **zero LLM
cost** and only messages when the disk is filling up. Do **not** install it
unprompted. When the user asks, follow [`references/install.md`](references/install.md)
exactly — it copies the script onto the volume and registers the cron.

## Notes
- Override the alert threshold with `DISK_ALERT_THRESHOLD` (percent) in the Hermes `.env`.
- The script is plain bash. If you ever adapt it to TypeScript, Hermes runs non-`.sh`
  scripts via Python — so a `.ts` body must be invoked through a `.sh` wrapper that
  calls `node` (the image ships Node 22). Point the cron's `--script` at the `.sh`.
</content>
