#!/usr/bin/env python3
"""ship-it-digest — daily GitHub activity briefing for a named environment.

Emits Slack mrkdwn (<url|text> links, :custom_emoji:) summarizing the last N hours
of a repo's activity: merged PRs, open PRs with their recent timeline events, and
issues with recent activity. Designed to run as a `no_agent` Hermes cron — its stdout
is delivered verbatim, zero LLM cost.

Hermes runs any non-.sh/.bash script under `sys.executable` (verified in
NousResearch/hermes-agent cron/scheduler.py), so this `.py` is the cron body directly
— no shell wrapper. It ALWAYS exits 0: per the no_agent delivery contract a non-zero
exit is delivered to the channel as an error alert, so a transient `gh`/network hiccup
must degrade to an inline note, not spam the channel.

Config is a single JSON file resolved next to this script: config.json (live) with a
fallback to config.enxample (committed template). It holds the environment->repos+token
table and the login->:emoji: roster — the engine itself is generic and edit-free.

Usage:  ship_it_digest.py [ENV_TYPE]        # env may also come from $ENV_TYPE
Env:    ENV_TYPE              which environment row to run (if not passed as arg)
        SHIPIT_WINDOW_HOURS   look-back window in hours (overrides config; default 24)
        <token env>           per-env token, named by config; falls back to TARGET_GH_TOKEN
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


class GhError(RuntimeError):
    """A `gh` invocation failed; surfaced inline, never fatal."""


# ── Config ────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    for name in ("config.json", "config.enxample"):
        path = SCRIPT_DIR / name
        if path.is_file():
            return json.loads(path.read_text())
    return {}


# ── gh helpers ──────────────────────────────────────────────────────────────--
def run_gh(args: list[str], token: str) -> str:
    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token
    try:
        proc = subprocess.run(
            ["gh", *args], capture_output=True, text=True, env=env, timeout=60
        )
    except FileNotFoundError as exc:  # gh not on PATH
        raise GhError("gh CLI not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"timed out: gh {' '.join(args)}") from exc
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip() or f"gh exited {proc.returncode}")
    return proc.stdout


def gh_json(args: list[str], token: str):
    out = run_gh(args, token).strip()
    return json.loads(out) if out else []


# ── Formatting ────────────────────────────────────────────────────────────────
def _actor(e: dict) -> str:
    return (e.get("actor") or {}).get("login") or "unknown"


def _login(user: dict | None) -> str:
    return (user or {}).get("login") or "unknown"


def _short(user: dict | None) -> str:
    """First word of display name, else login — matches the original briefing style."""
    user = user or {}
    return (user.get("name") or user.get("login") or "unknown").split(" ")[0]


# One source of truth for event -> line, shared by PRs and issues. Unknown events
# fall through to a generic line rather than being dropped.
EVENT_FMT = {
    "labeled": lambda e: f"🏷️  Labeled {(e.get('label') or {}).get('name', '?')} by {_actor(e)}",
    "unlabeled": lambda e: f"🏷️  Unlabeled {(e.get('label') or {}).get('name', '?')} by {_actor(e)}",
    "assigned": lambda e: f"👤 Assigned to {_login(e.get('assignee'))} by {_actor(e)}",
    "unassigned": lambda e: f"👤 Unassigned {_login(e.get('assignee'))} by {_actor(e)}",
    "mentioned": lambda e: f"💬 Mentioned by {_actor(e)}",
    "referenced": lambda e: f"🔗 Referenced in commit by {_actor(e)}",
    "review_requested": lambda e: f"👀 Review requested from {_login(e.get('requested_reviewer'))} by {_actor(e)}",
    "review_request_removed": lambda e: f"👀 Review request removed from {_login(e.get('requested_reviewer'))} by {_actor(e)}",
    "ready_for_review": lambda e: f"✅ Ready for review by {_actor(e)}",
    "convert_to_draft": lambda e: f"✏️ Converted to draft by {_actor(e)}",
    "closed": lambda e: f"✅ Closed by {_actor(e)}",
    "reopened": lambda e: f"🔄 Reopened by {_actor(e)}",
    "renamed": lambda e: f"✏️ Renamed by {_actor(e)}",
    "milestoned": lambda e: f"🎯 Milestoned by {_actor(e)}",
    "demilestoned": lambda e: f"🎯 Demilestoned by {_actor(e)}",
    "head_ref_deleted": lambda e: f"🔀 Branch deleted by {_actor(e)}",
    "head_ref_restored": lambda e: f"🔀 Branch restored by {_actor(e)}",
}

REVIEW_FMT = {
    "APPROVED": lambda r: f"✅ Approved by {_login(r.get('user'))}",
    "CHANGES_REQUESTED": lambda r: f"❌ Changes requested by {_login(r.get('user'))}",
    "COMMENTED": lambda r: f"💬 Review comment by {_login(r.get('user'))}",
}


def fmt_event(e: dict) -> str:
    fn = EVENT_FMT.get(e.get("event"))
    return fn(e) if fn else f"🔄 {e.get('event', 'event')} by {_actor(e)}"


def fmt_review(r: dict) -> str:
    fn = REVIEW_FMT.get(r.get("state"))
    return fn(r) if fn else f"🔄 Review {r.get('state', '?')} by {_login(r.get('user'))}"


def make_emojify(roster: dict):
    if not roster:
        return lambda s: s
    keys = sorted(roster, key=len, reverse=True)  # longest first: NinadMaladkar > Ninad
    pat = re.compile(r"\b(" + "|".join(re.escape(k) for k in keys) + r")\b")
    return lambda s: pat.sub(lambda m: roster[m.group(0)], s)


# ── Engine ──────────────────────────────────────────────────────────────────--
def main() -> int:
    cfg = load_config()
    envs = cfg.get("environments", {})
    emojify = make_emojify(cfg.get("roster", {}))

    env_type = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ENV_TYPE", "")).strip()
    if not env_type or env_type not in envs:
        known = ", ".join(envs) or "none"
        print(f"ship-it-digest: unknown or missing ENV_TYPE '{env_type}'. Known: {known}",
              file=sys.stderr)
        return 2  # config error before any delivery — fine to be non-zero here

    repos = envs[env_type].get("repos", [])
    token = os.environ.get(envs[env_type].get("token_env", ""), "") or os.environ.get("TARGET_GH_TOKEN", "")
    window = int(os.environ.get("SHIPIT_WINDOW_HOURS", cfg.get("window_hours", 24)))
    since = (datetime.now(timezone.utc) - timedelta(hours=window)).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines: list[str] = [f"🚢 Daily Ship-It Briefing — {env_type.upper()} (last {window}h)"]

    def emit(text: str, indent: int = 0) -> None:
        lines.append(" " * indent + emojify(text))

    activity = False

    for repo in repos:
        try:
            run_gh(["repo", "view", repo, "--json", "name"], token)
        except GhError as exc:
            emit(f"\n⚠️  Cannot access repository {repo}: {exc}")
            continue

        emit(f"\n--- Repository: {repo} ---")

        # Merged PRs
        try:
            merged = gh_json(
                ["pr", "list", "-R", repo, "--state", "merged",
                 "--search", f"merged:>={since}",
                 "--json", "number,title,author,url,mergedBy"], token)
            if merged:
                emit("\n✅ Merged PRs:")
                for pr in merged:
                    emit(f"  • <{pr['url']}|#{pr['number']}> {pr['title']} "
                         f"({_short(pr.get('author'))}/{_short(pr.get('mergedBy'))})")
                activity = True
        except GhError as exc:
            emit(f"  ⚠️  merged-PR fetch failed: {exc}")

        # Open PRs + per-PR timeline activity
        try:
            open_prs = gh_json(
                ["pr", "list", "-R", repo, "--state", "open",
                 "--json", "number,title,author,url,updatedAt,createdAt"], token)
        except GhError as exc:
            open_prs = []
            emit(f"  ⚠️  open-PR fetch failed: {exc}")

        if open_prs:
            emit("\n⏳ Currently Open PRs:")
            for pr in open_prs:
                tag = "NEW: " if pr["createdAt"] >= since else ""
                emit(f"  • <{pr['url']}|#{pr['number']}> {tag}{pr['title']} ({_short(pr.get('author'))})")
                if pr["updatedAt"] < since:
                    continue
                acts: list[str] = []
                try:
                    for e in gh_json(["api", f"repos/{repo}/issues/{pr['number']}/events"], token):
                        if e.get("created_at", "") >= since:
                            acts.append(fmt_event(e))
                    for c in gh_json(["api", f"repos/{repo}/issues/{pr['number']}/comments"], token):
                        if c.get("created_at", "") >= since:
                            acts.append(f"💬 Comment by {_login(c.get('user'))}")
                    for r in gh_json(["api", f"repos/{repo}/pulls/{pr['number']}/reviews"], token):
                        if (r.get("submitted_at") or "") >= since:
                            acts.append(fmt_review(r))
                except GhError as exc:
                    emit(f"      ⚠️  activity fetch failed: {exc}", )
                for line in sorted(set(acts))[:5]:  # dedup (reviews echo comments), cap noise
                    emit(line, indent=6)
            activity = True

        # Issues with recent activity
        try:
            issues_enabled = gh_json(["repo", "view", repo, "--json", "hasIssuesEnabled"], token)
        except GhError:
            issues_enabled = {}
        if isinstance(issues_enabled, dict) and issues_enabled.get("hasIssuesEnabled"):
            try:
                issues = gh_json(
                    ["issue", "list", "-R", repo, "--search", f"updated:>={since}",
                     "--json", "number,title,url,state,updatedAt,createdAt,author",
                     "--limit", "20"], token)
            except GhError as exc:
                issues = []
                emit(f"  ⚠️  issue fetch failed: {exc}")

            if issues:
                emit("\n📝 Activity Updates:")
                for it in issues:
                    author = _login(it.get("author"))
                    tag = "NEW: " if it["createdAt"] >= since else ""
                    emit(f"  • <{it['url']}|#{it['number']}> {tag}{it['title']} ({author})")
                    events: list[str] = []
                    try:
                        for e in gh_json(["api", f"repos/{repo}/issues/{it['number']}/events"], token):
                            if e.get("created_at", "") >= since:
                                events.append(fmt_event(e))
                    except GhError as exc:
                        emit(f"    ⚠️  event fetch failed: {exc}")
                    if events:
                        for line in events:
                            emit(line, indent=4)
                    elif it["createdAt"] >= since:
                        emit(f"🆕 Created by {author}", indent=4)
                    else:
                        try:
                            comments = [c for c in gh_json(
                                ["api", f"repos/{repo}/issues/{it['number']}/comments"], token)
                                if c.get("created_at", "") >= since]
                        except GhError:
                            comments = []
                        if comments:
                            for c in comments:
                                emit(f"💬 Comment by {_login(c.get('user'))}", indent=4)
                        else:
                            emit("🔄 Updated", indent=4)
                    activity = True

    if not activity:
        emit(f"\n(No activity in the last {window}h)")

    print("\n".join(lines))
    return 0  # ALWAYS 0: a digest must never deliver as an error alert.


if __name__ == "__main__":
    sys.exit(main())
