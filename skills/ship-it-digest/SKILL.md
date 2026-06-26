---
name: ship-it-digest
description: Post a daily "Ship-It" briefing for a GitHub environment — merged PRs, open PRs with their latest review/comment activity, and recently-touched issues — into a Slack channel. Use when a user asks for a daily standup digest, a "what shipped" / ship-it report, or to schedule a recurring repo-activity summary. Ships a zero-LLM-cost script the agent installs as a cron on request.
version: 1.0.0
license: MIT
metadata:
  hermes:
    tags: [ops, github, reporting, cron]
---

# Ship-It Digest

Summarize the last N hours (default 24) of GitHub activity for a **named environment**
— a label like `web` or `api` that maps to one or more repos plus a token. Output is
**Slack mrkdwn** (`<url|text>` links, `:custom_emoji:`) and covers:

- ✅ **Merged PRs** in the window,
- ⏳ **Open PRs** with their recent timeline events (reviews, comments, labels, …),
- 📝 **Issues** with activity in the window.

The bundled script is pure bash + `gh` + `jq` — no LLM in the loop — so it runs as a
`no_agent` cron (verbatim delivery, zero cost), same pattern as `disk-watchdog`.

## Configuration (single source of truth)
All environment-specific facts live in **two sourced config files** next to the
script, never in the script body:

- `assets/environments.conf` — `ENV_TYPE` → repo list + token-var name.
- `assets/roster.conf` — GitHub login/name → Slack `:emoji:` (optional; unmatched
  names render as raw logins).

Each ships as a `.enxample` template; copy to `.conf` and edit. Onboard a new
environment or team member by adding a row — the engine (`ship_it_digest.sh`) stays
edit-free. Override the look-back window with `SHIPIT_WINDOW_HOURS`.

## 1. One-off run (do this directly)
```
ENV_TYPE=web bash {skill_dir}/assets/ship_it_digest.sh
```
Or pass the environment as an argument: `bash {skill_dir}/assets/ship_it_digest.sh web`.
The token comes from the env var named in `environments.conf` (e.g. `GH_TOKEN_WEB`),
falling back to `TARGET_GH_TOKEN`. Report the output back to the user.

## 2. Recurring digest (only when the user asks to "schedule" / "set up" it)
Installs a `no_agent` cron that posts the digest on a schedule with **zero LLM cost**.
Do **not** install it unprompted. When the user asks, follow
[`references/install.md`](references/install.md) exactly — it copies the script **and
its config files** onto the volume and registers one cron per environment.

## 3. Optional: agent-summarized digest
The default cron posts the raw briefing verbatim. If the user instead wants the
runtime model to **triage and summarize** it (e.g. "just tell me what needs my
attention"), register an *agent* cron that runs the script and feeds its output to a
prompt — this costs one LLM call per run. The install reference documents this
variant under "Optional: agent-summarized mode." Default to the verbatim `no_agent`
form unless the user explicitly wants prioritization.

## Notes
- **Requires bash 4+** (`declare -gA` associative arrays). The Hermes image (Debian,
  bash 5) is fine; macOS's stock bash 3.2 is not — test in the container, not locally.
- Needs `gh` and `jq` on PATH (both present in the Hermes image) and a token with
  read access to the target repos.
- The script is plain bash. If you adapt it to TypeScript, Hermes runs non-`.sh`
  scripts via Python — wrap a `.ts` body in a `.sh` that calls `node` and point the
  cron's `--script` at the `.sh`.
