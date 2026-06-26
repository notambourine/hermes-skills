---
name: update-ntb-skills
description: Refresh the NoTambourine team skills (this external skills library) to the latest published version. Use when the user says "update skills", "update ntb skills", "pull the latest skills", or just published a new/changed skill to the hermes-skills repo and wants this agent to pick it up.
version: 1.0.0
license: MIT
metadata:
  hermes:
    tags: [ops, skills, maintenance]
---

# Update NoTambourine Skills

This agent loads the team skill library **read-only** from a git checkout on the
`/data` volume, registered as `skills.external_dirs` in `config.yaml`. The
[`hermes-agent-template`](https://github.com/notambourine/hermes-agent-template)
clones it on first boot and fast-forwards it on every restart; this skill pulls
the latest published version **mid-session, without a restart** — including this
skill itself.

## Refresh now (do this directly when asked)

Fast-forward the checkout to the latest published ref and report the new HEAD:

```bash
DIR="${HERMES_SKILLS_DIR:-/data/.hermes/external-skills}"
REF="${HERMES_SKILLS_REF:-main}"
git -C "$DIR" fetch --depth 1 origin "$REF" && git -C "$DIR" reset --hard FETCH_HEAD
git -C "$DIR" log -1 --format='Now at %h — %s (%cr)'
```

Report that final line back to the user. The next tool-registry scan picks up
added or changed skills automatically. If a skill still looks stale in the same
session, tell the user that the dashboard's **Restart Gateway** button forces a
clean re-scan (it restarts the container, which also re-pulls on boot).

## If the checkout is missing (first-boot clone failed)

```bash
DIR="${HERMES_SKILLS_DIR:-/data/.hermes/external-skills}"
REF="${HERMES_SKILLS_REF:-main}"
git clone --depth 1 --branch "$REF" \
  https://github.com/notambourine/hermes-skills.git "$DIR"
```

## Notes

- This **never commits or pushes** — the checkout is a read-only mirror. `reset
  --hard` is intentional: it tracks the remote ref cleanly even after a
  force-push or rebased history, where a plain `git pull` would fail.
- It does **not** touch agent- or dashboard-authored skills on `/data` — those
  live under `$HERMES_HOME/skills`, a separate discovery root Hermes scans
  alongside this one.
