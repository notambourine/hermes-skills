# Install the ship-it-digest as a recurring cron

Run these steps only when the user asks to schedule / set up the digest. Confirm the
**environment** (e.g. `web`) and the **target Slack channel** first.

## 1. Copy the script and its config onto the volume
Hermes resolves `hermes cron --script` paths under `~/.hermes/scripts/`, and the
engine sources its config from its own directory — so the script **and** both config
files must land there together (the skill ships them read-only under the image's
skill dir):

```bash
mkdir -p ~/.hermes/scripts
cp {skill_dir}/assets/ship_it_digest.sh ~/.hermes/scripts/ship_it_digest.sh

# Seed config from templates on first install; never clobber an edited .conf.
for f in environments roster; do
  [ -f ~/.hermes/scripts/$f.conf ] || \
    cp {skill_dir}/assets/$f.enxample ~/.hermes/scripts/$f.conf
done
```

Then verify `environments.conf` has a row for the requested env (and `roster.conf`
has the team's emoji). Edit those `.conf` files — never the engine.

## 2. Create a per-environment wrapper
`hermes cron --script` points at a single path with no arguments, so bind the
environment with a tiny wrapper that sets `ENV_TYPE` and execs the engine **relative
to itself** (no hardcoded volume path):

```bash
cat > ~/.hermes/scripts/ship_it_web.sh <<'EOF'
#!/usr/bin/env bash
export ENV_TYPE=web
exec "$(dirname "$0")/ship_it_digest.sh"
EOF
chmod +x ~/.hermes/scripts/ship_it_web.sh
```

Repeat with a new filename + `ENV_TYPE` for each environment.

## 3. Register the cron (verbatim `no_agent` — default)
Check first so you don't create a duplicate:

```bash
hermes cron list --all | grep -q 'ship-it-web' || \
  hermes cron create '0 13 * * 1-5' \
    --name ship-it-web \
    --script ship_it_web.sh \
    --no-agent \
    --deliver slack:CHANNEL_ID
```

- `--no-agent` → the script *is* the job; its stdout is delivered verbatim, zero LLM cost.
- `'0 13 * * 1-5'` → weekday mornings (UTC); adjust to taste (`'every 24h'`, etc.).
- `--deliver slack:CHANNEL_ID` → post to a specific Slack channel. Use `--deliver origin`
  to reply to whoever asked, or `platform:chat_id` for another target.

## Optional: agent-summarized mode
If the user wants the runtime model to triage rather than dump the full briefing,
drop `--no-agent` and give the agent a prompt (this costs one LLM call per run):

```bash
hermes cron create '0 13 * * 1-5' \
  --name ship-it-web-summary \
  --script ship_it_web.sh \
  --deliver slack:CHANNEL_ID \
  --prompt 'Run the script. From its output, post a short Slack message highlighting
            only what needs human attention today: PRs awaiting review, stalled open
            PRs, and newly-opened issues. Keep Slack <url|text> links and :emoji:.'
```

## Confirm
Report the created job (`hermes cron list`) back to the user. The `no_agent` form
always posts (it prints a "(No activity…)" line on a quiet day rather than going
silent — unlike a watchdog, a daily digest is expected to show up).

## Removing it later
```bash
hermes cron list --all          # find the job id
hermes cron remove <job_id>
```
