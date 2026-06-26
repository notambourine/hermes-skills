# hermes-skills

Repo-managed, importable **skills** for the [Hermes Agent Railway template](https://github.com/notambourine/hermes-agent-template).

Skills are authored here with a capable model, reviewed in PRs, and baked into the
deployed image at build time. The Hermes runtime model only ever *consumes* them —
it never authors them. That's the point: quality is locked in at build, not
regenerated at runtime by a small free-tier model.

## How the template imports these skills

The template's `Dockerfile` clones this repo to `/opt/hermes-skills` at build time,
and `start.sh` registers `skills/` as a **read-only external discovery root** via
`config.yaml` → `skills.external_dirs`. Hermes scans it alongside the volume's own
`/data/.hermes/skills/`, so:

- repo skills stay **immutable image content** (a redeploy rolls them forward),
- the **volume is never touched** — dashboard- or agent-authored skills keep living on `/data`.

That's the entire integration: import skills, nothing more.

> **Build dependency:** the template's image build clones this repo from
> `github.com/notambourine/hermes-skills`. It must exist and be reachable before a
> template build will succeed. Pin a specific version per build with
> `--build-arg HERMES_SKILLS_REF=<tag>`.

## Layout

```
skills/<name>/SKILL.md       Anthropic / agentskills.io skill format (YAML frontmatter + body)
            references/      supporting docs, loaded on demand (progressive disclosure)
            assets/          scripts/templates a skill ships (e.g. a cron body)
```

## Authoring skills

A skill is a directory with a `SKILL.md`. Frontmatter (`name` ≤64 chars,
`description` ≤1024 chars) is the tier-1 metadata the agent sees first; the body and
`references/` / `assets/` files load only when the skill is invoked. Write for a
**small model**: be prescriptive, give exact steps and an output template, leave
little to infer. See `skills/web-brief/` for a worked example.

## Crons are agent-installed, not baked

This repo does **not** create cron jobs at deploy time. If a skill benefits from a
recurring job, it ships the script under its `assets/` and an install reference that
tells the agent how to set it up — the agent copies the script to the volume's
`/data/.hermes/scripts/` (pinned via `${HERMES_HOME:-/data/.hermes}`, never `~` — the
agent shell's `$HOME` is the ephemeral `/root`, not the volume) and runs `hermes cron
create` **only when the user asks**. See
`skills/disk-watchdog/` (`references/install.md`) for the pattern, and
`skills/ship-it-digest/` for a config-driven multi-environment variant.

Why agent-installed rather than reconciled on boot: cron creation then happens in a
real turn where the agent can confirm intent and target channel, instead of racing
container boot — and there's no brittle "does this job already exist" parsing in
`start.sh`.

### TypeScript cron bodies must be `.sh`-wrapped

Hermes runs `.sh`/`.bash` scripts via **bash** and **every other extension via
Python** — a `.ts` file pointed at directly would be handed to Python and fail. Wrap
TypeScript in a `.sh` that invokes Node (the image ships Node 22) and point the
cron's `--script` at the `.sh`.
</content>
