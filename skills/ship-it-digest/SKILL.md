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
- 📝 **Issue Activity** — issues touched in the window, each a one-line summary
  (`#num title (reporter → assignees) 🏷️ labels`) with movement-only sub-bullets
  (status journey, commits per author, comments). Grouped **Active / New** by default,
  or **by board column** when the environment pins a ProjectV2 board (see below).

The bundled engine is a single Python file (`assets/ship_it_digest.py`) using only the
stdlib plus the `gh` CLI — no LLM in the loop — so it runs as a `no_agent` cron
(verbatim delivery, zero cost), same pattern as `disk-watchdog`. Hermes runs any
non-`.sh`/`.bash` script under its own `sys.executable`, so the `.py` is the cron body
directly — **no shell wrapper**.

## Configuration (single source of truth)
All environment-specific facts live in **one JSON file** next to the engine, never in
the engine body:

- `assets/config.json` — the live config (gitignored). Holds three keys:
  - `environments` — `ENV_TYPE` → `{ repos: [...], token_env: "GH_TOKEN_…", board?: "PVT_…" }`.
    The optional `board` pins **one** ProjectV2 board (by GraphQL node id) per env — one
    board per org, shared across all the env's repos — to group Issue Activity by its
    columns (order fetched live each run). Omit it to group Active/New. **`board`
    requires a token with `Projects: read` scope**; with a scopeless token issues
    degrade to a "No status" group rather than failing.
  - `roster` — GitHub login / display-name first-word → Slack `:custom_emoji:` (optional; unmatched names render as raw logins)
  - `window_hours` — default look-back window
- `assets/config.enxample` — the committed template with placeholder values. The engine
  reads `config.json` if present, else falls back to this template.

Copy `config.enxample` → `config.json` and edit. Onboard a new environment or team
member by adding a row — the engine stays edit-free. Override the look-back window
per-run with `SHIPIT_WINDOW_HOURS`.

## 1. One-off run (do this directly)
```
ENV_TYPE=web python3 {skill_dir}/assets/ship_it_digest.py
```
Or pass the environment as an argument: `python3 {skill_dir}/assets/ship_it_digest.py web`.
The token comes from the env var named in `config.json` (e.g. `GH_TOKEN_WEB`), falling
back to `TARGET_GH_TOKEN`. Report the output back to the user.

## 2. Recurring digest (only when the user asks to "schedule" / "set up" it)
Installs a `no_agent` cron that posts the digest on a schedule with **zero LLM cost**.
Do **not** install it unprompted. When the user asks, follow
[`references/install.md`](references/install.md) exactly — it copies the engine **and
its config** onto the volume and registers one cron per environment.

## 3. Optional: agent-summarized digest
The default cron posts the raw briefing verbatim. If the user instead wants the
runtime model to **triage and summarize** it (e.g. "just tell me what needs my
attention"), register an *agent* cron that runs the script and feeds its output to a
prompt — this costs one LLM call per run. The install reference documents this
variant under "Optional: agent-summarized mode." Default to the verbatim `no_agent`
form unless the user explicitly wants prioritization.

## Notes
- **Exits 0 always.** Per the `no_agent` delivery contract a non-zero exit is delivered
  to the channel as an error alert, so a transient `gh`/network hiccup degrades to an
  inline note rather than spamming the channel. (The one exception: an unknown/missing
  `ENV_TYPE` exits 2 — that's a config error caught *before* any delivery.)
- Needs `gh` on PATH (present in the Hermes image) and a token with read access to the
  target repos. No `jq` required — the engine parses JSON in Python.
- Pure stdlib; runs on any Python 3 in the image. No third-party packages.
