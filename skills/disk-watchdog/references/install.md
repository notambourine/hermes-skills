# Install the disk-watchdog as a recurring cron

Run these steps only when the user asks to schedule / set up the watchdog.

## 1. Copy the script onto the volume
Hermes resolves `hermes cron --script` paths under `~/.hermes/scripts/`, so the
script must live there (the skill ships it read-only under the image's skill dir):

```bash
mkdir -p ~/.hermes/scripts
cp {skill_dir}/assets/disk_watchdog.sh ~/.hermes/scripts/disk_watchdog.sh
```

## 2. Create the cron (only if it doesn't already exist)
Check first so you don't create a duplicate:

```bash
hermes cron list --all | grep -q 'disk-watchdog' || \
  hermes cron create '*/15 * * * *' \
    --name disk-watchdog \
    --script disk_watchdog.sh \
    --no-agent \
    --deliver origin
```

- `--no-agent` → the script *is* the job; its stdout is delivered verbatim, zero LLM cost.
- `--deliver origin` → alerts go back to whoever asked. Use `--deliver telegram` (etc.)
  or `platform:chat_id` to target a specific channel.
- Adjust the schedule (`'*/15 * * * *'`, `'30m'`, `'every 6h'`) to taste.

## 3. Confirm
Report the created job (`hermes cron list`) back to the user, and note that it will
stay silent unless usage crosses the threshold.

## Removing it later
```bash
hermes cron list --all          # find the job id
hermes cron remove <job_id>
```
</content>
