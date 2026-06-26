# Install the ship-it-digest as a recurring cron

Run these steps only when the user asks to schedule / set up the digest. Confirm the
**environment** (e.g. `web`) and the **target Slack channel** first.

## 1. Copy the engine and seed its config onto the volume
The engine resolves `config.json` from its own directory, so the engine **and** its
config must land in the **same** directory on the **persistent volume** (the skill ships
them read-only under the image's skill dir).

> ⚠️ **Do not use `~` or `$HOME` here.** Your interactive shell runs with `HOME=/root`
> (ephemeral container root), but the Hermes runtime — and the volume the cron actually
> executes from — lives under `/data/.hermes`. A `cp … ~/.hermes/scripts/` writes to the
> wrong, throwaway `/root` and silently splits the engine from its config. Always pin the
> volume path explicitly, exactly as `update-ntb-skills` does with `$HERMES_SKILLS_DIR`:

```bash
# Resolve the volume's scripts dir once; reuse for every step below.
HSCRIPTS="${HERMES_HOME:-/data/.hermes}/scripts"
mkdir -p "$HSCRIPTS"
cp {skill_dir}/assets/ship_it_digest.py "$HSCRIPTS/ship_it_digest.py"

# Seed config from the template on first install; never clobber an edited config.json.
[ -f "$HSCRIPTS/config.json" ] || \
  cp {skill_dir}/assets/config.enxample "$HSCRIPTS/config.json"

# Guard against the split: both files MUST be co-located, or the engine can't find config.
[ -f "$HSCRIPTS/ship_it_digest.py" ] && [ -f "$HSCRIPTS/config.json" ] \
  && echo "OK — engine + config co-located in $HSCRIPTS" \
  || { echo "SPLIT — engine and config are not in $HSCRIPTS; do NOT proceed"; }
```

Then verify `config.json` has an `environments` row for the requested env (and a
`roster` entry per teammate). Edit `config.json` — never the engine.

## 2. Confirm the token is reachable
The GitHub token named by `config.json` (e.g. `GH_TOKEN_WEB`) is **already provided by the
environment** — assume it exists; do not write it. The scheduler passes the gateway
environment through to the subprocess: it runs through `_sanitize_subprocess_env`, which
strips only Hermes **provider** secrets, so your `GH_TOKEN_*` survives. Just verify it's
visible before scheduling:

```bash
[ -n "$GH_TOKEN_WEB" ] && echo 'token present' || echo 'MISSING — ask the operator to set it'
```

If the environment pins a `board` in `config.json` (to group Issue Activity by ProjectV2
column), that token **must include `Projects: read` scope** — find the board id with
`gh api graphql -f query='{organization(login:"your-org"){projectV2(number:1){id}}}'` and
put it in the env's config row. Without project scope the digest still posts; issues just
fall into a "No status" group.

## 3. Bind the environment with a one-line launcher (one per env)
`hermes cron --script` points at a single path with **no arguments** (the scheduler
invokes `argv=[interp, path]`), so the environment can't ride in on the command line.
Bind it with a minimal `.py` launcher that sets `ENV_TYPE` and re-execs the engine
**relative to itself**:

```bash
cat > "${HERMES_HOME:-/data/.hermes}/scripts/ship_it_web.py" <<'EOF'
#!/usr/bin/env python3
import os, sys
os.environ["ENV_TYPE"] = "web"
engine = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ship_it_digest.py")
os.execv(sys.executable, [sys.executable, engine])
EOF
```

Use this **even for a single-environment deployment** — it's the one pattern, not a
multi-env special case. Do **not** set `ENV_TYPE` in the volume's shared `.env`: that
exports it process-wide to *every* cron the runtime launches, so a second
`ENV_TYPE`-reading skill (or a second ship-it env) silently inherits the wrong value.
The launcher scopes `ENV_TYPE` to its own invocation, and resolving the engine via
`__file__` means it can never split from the engine the way a hardcoded `~` path can.
(`os.execv` replaces the process, so the engine's exit code — and the always-0 delivery
contract — pass straight through.)

For more environments, repeat with a new filename + `ENV_TYPE` (`ship_it_api.py`, …).

## 4. Register the cron (verbatim `no_agent` — default)
Check first so you don't create a duplicate. Point `--script` at the **launcher** from
step 3, never the bare engine (the engine has no env bound, so it'd exit 2 on an unknown
`ENV_TYPE`):

```bash
hermes cron list --all | grep -q 'ship-it-web' || \
  hermes cron create '0 13 * * 1-5' \
    --name ship-it-web \
    --script ship_it_web.py \
    --no-agent \
    --deliver slack:CHANNEL_ID
```

- `--no-agent` → the script *is* the job; its stdout is delivered verbatim, zero LLM cost.
- `'0 13 * * 1-5'` → weekday mornings (UTC); adjust to taste (`'every 24h'`, etc.).
- `--deliver slack:CHANNEL_ID` → post to a specific Slack channel. Use `--deliver origin`
  to reply to whoever asked, or `platform:chat_id` for another target.

## 5. Test it immediately
Force a single run without waiting for the schedule, then read the delivered output:

```bash
hermes cron list                 # find the job id
hermes cron run <job_id>         # fire once now
```

## Optional: agent-summarized mode
If the user wants the runtime model to triage rather than dump the full briefing,
drop `--no-agent` and give the agent a prompt (this costs one LLM call per run):

```bash
hermes cron create '0 13 * * 1-5' \
  --name ship-it-web-summary \
  --script ship_it_web.py \
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
