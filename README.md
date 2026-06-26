# hermes-skills

Repo-managed **skills** and **cron jobs** for the [Hermes Agent Railway template](https://github.com/notambourine/hermes-agent-template).

These are authored here with a capable model, reviewed in PRs, and baked into the
deployed image at build time. The Hermes runtime model only ever *consumes* them —
it never authors them. That's the point: quality is locked in at build, not
regenerated at runtime by a small free-tier model.

## How the template consumes this repo

The template's `Dockerfile` clones this repo to `/opt/hermes-skills` at build time,
and `start.sh` wires it up on each boot:

| Path here | Where it goes | Mechanism |
|---|---|---|
| `skills/<name>/SKILL.md` | scanned in place, read-only | registered via `config.yaml` → `skills.external_dirs` |
| `scripts/*` | copied to `/data/.hermes/scripts/` | `cp` on boot (code, not user data — overwritten) |
| `crons.json` | reconciled into Hermes' scheduler | `hermes cron create`, **create-if-absent by name** |

Skills are registered as a **read-only external dir**, so the persistent volume's
own `/data/.hermes/skills/` (dashboard- or agent-authored skills) is never touched.

> **Build dependency:** the template's image build clones this repo from
> `github.com/notambourine/hermes-skills`. It must exist and be pushed before a
> template build will succeed. Pin a specific version per build with
> `--build-arg HERMES_SKILLS_REF=<tag>`.

## Layout

```
skills/<name>/SKILL.md      Anthropic / agentskills.io skill format (YAML frontmatter + body)
            references/     optional supporting docs, loaded on demand (progressive disclosure)
scripts/                    cron job bodies
crons.json                  declarative cron manifest, reconciled on boot
```

## Authoring skills

A skill is a directory with a `SKILL.md`. Frontmatter (`name` ≤64 chars,
`description` ≤1024 chars) is what the agent sees first (tier-1 metadata); the body
and `references/` files load only when the skill is invoked. Write for a **small
model**: be prescriptive, give exact steps and an output template, leave little to
infer. See `skills/web-brief/` for a worked example.

## Authoring cron jobs

Add a script under `scripts/` and an entry to `crons.json`:

```json
{
  "jobs": [
    { "name": "disk-watchdog", "schedule": "*/15 * * * *", "script": "disk_watchdog.sh", "no_agent": true, "deliver": "local" }
  ]
}
```

Fields: `name` (required, also the idempotency key), `schedule` (`"30m"`,
`"every 6h"`, `"0 9 * * *"`), `script` (file under `scripts/`), `no_agent`
(`true` = run the script and deliver stdout verbatim, **zero LLM cost**; empty
stdout = silent), `prompt` / `skills` (for agent-driven jobs), `deliver`, `repeat`.

### TypeScript crons must be `.sh`-wrapped

Hermes runs `.sh`/`.bash` scripts via **bash** and **every other extension via
Python**. A `.ts` file pointed at directly would be handed to Python and fail.
Run TypeScript through a bash wrapper that invokes Node (the image ships Node 22) —
see `scripts/ts_cron_example.sh`. Point the cron's `script` at the **`.sh`**, never
the `.ts`.

### Agent-driven cron (uses a skill)

Omit `no_agent` and give a `prompt` + `skills` to have the LLM run on a schedule —
this **does** cost tokens each run:

```json
{ "name": "morning-brief", "schedule": "0 13 * * *", "prompt": "Brief me on AI news from the last 24h.", "skills": ["web-brief"], "deliver": "telegram" }
```
