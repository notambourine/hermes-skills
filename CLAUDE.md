# hermes-skills — operating context

Repo-managed **skills** baked into the [Hermes Agent](https://github.com/notambourine/hermes-agent-template)
image at build time. Skills are authored here with a capable model and reviewed in PRs;
the Hermes runtime model only ever *consumes* them. See `README.md` for the import
mechanism and skill-authoring format — this file is the always-on agent context that
supplements it.

## Hermes runtime contract (the rules a skill's scripts must obey)

- **Script dispatch by extension.** Hermes runs `.sh`/`.bash` via **bash** and **every
  other extension via Python** (`sys.executable`). A `.py` cron body is invoked directly
  as `argv=[interp, path]` with **no arguments** — pass per-run config via env vars, not
  argv, for crons. A `.ts` body must be `.sh`-wrapped (Node 22 is in the image).
- **`no_agent` crons must exit 0.** Their stdout is delivered verbatim; a non-zero exit is
  delivered to the channel as an *error alert*. So a transient `gh`/network failure must
  degrade to an inline note, never a non-zero exit. The only acceptable non-zero is a
  config error caught *before* any delivery (e.g. unknown `ENV_TYPE`).
- **Crons are agent-installed, not baked.** Skills ship a script under `assets/` plus a
  `references/install.md`; the agent installs the cron only when the user asks.

## Conventions

- **Single source of truth.** Environment-specific facts (repos, tokens, rosters, board
  ids) live in a skill's JSON/config next to its engine — never hard-coded in the engine
  body. Onboarding a repo or teammate is a config edit, not a code edit.
- **Write skills for a small model.** Be prescriptive: exact steps, an output template,
  little left to infer.
- **Brand name:** `NoTambourine` in prose, `notambourine` in backticks/slugs (per the
  parent `notambourine/CLAUDE.md`). Never the sentence-case `Notambourine`.

## Secret & local-file hygiene

Never commit secrets or real environment config. The following are gitignored and stay
local — see **`CLAUDE.local.md`** for what each one holds and how to run the engine
locally:

- `.env` — real per-environment GitHub tokens.
- `**/config.json` — live skill config (real repo/org/board names).
- `*.local.*` (e.g. `example.local.md`, `runner.local.sh`, `CLAUDE.local.md`) — local
  scaffolding and sample output.

Only `*.enxample` templates (with placeholder values) are committed. `.env.example` is
deny-blocked by tooling — use the `.enxample` suffix.
