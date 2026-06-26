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
def _gh_raw(args: list[str], token: str, timeout: int) -> subprocess.CompletedProcess:
    """Run `gh` with the token injected, converting the two 'gh itself failed' cases
    (missing binary, timeout) into GhError. Return-code / output policy is the caller's
    — run_gh treats a non-zero exit as fatal; gh_graphql tolerates it (partial errors)."""
    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token
    try:
        return subprocess.run(
            ["gh", *args], capture_output=True, text=True, env=env, timeout=timeout
        )
    except FileNotFoundError as exc:  # gh not on PATH
        raise GhError("gh CLI not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GhError(f"timed out: gh {' '.join(args)[:80]}") from exc


def run_gh(args: list[str], token: str) -> str:
    proc = _gh_raw(args, token, timeout=60)
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip() or f"gh exited {proc.returncode}")
    return proc.stdout


def gh_json(args: list[str], token: str):
    out = run_gh(args, token).strip()
    return json.loads(out) if out else []


def gh_graphql(query: str, token: str) -> dict:
    """Run a GraphQL query, returning the parsed body *even when GitHub reports
    partial errors* (e.g. project fields are FORBIDDEN without a Projects:read token).
    This lets callers degrade gracefully — keep the issues that resolved, drop the
    columns that didn't — instead of losing the whole section."""
    proc = _gh_raw(["api", "graphql", "-f", f"query={query}"], token, timeout=90)
    out = (proc.stdout or "").strip()
    if not out:
        raise GhError(proc.stderr.strip() or f"gh graphql exited {proc.returncode}")
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise GhError(f"unparseable graphql response: {out[:160]}") from exc


# ── Formatting ────────────────────────────────────────────────────────────────
def _actor(e: dict) -> str:
    return (e.get("actor") or {}).get("login") or "unknown"


def _login(user: dict | None) -> str:
    return (user or {}).get("login") or "unknown"


def _short(user: dict | None) -> str:
    """First word of display name, else login — matches the original briefing style."""
    user = user or {}
    return (user.get("name") or user.get("login") or "unknown").split(" ")[0]


def _is_bot(login: str) -> bool:
    """GitHub renders every bot account's login as 'name[bot]' in events/comments
    (e.g. 'railway-app[bot]'). We hide bot-authored timeline noise — automated
    deploys and comments — from the digest, which is about human activity."""
    return login.endswith("[bot]")


# One source of truth for event -> line, shared by PRs and issues. Sub-bullet lines carry
# NO emoji prefix — the parent section title owns the emoji. Unknown events fall through to
# a generic line (and flag the gap to map).
EVENT_FMT = {
    # PR / branch lifecycle
    "review_requested": lambda e: f"Review requested from {_login(e.get('requested_reviewer'))} by {_actor(e)}",
    "review_request_removed": lambda e: f"Review request removed from {_login(e.get('requested_reviewer'))} by {_actor(e)}",
    "ready_for_review": lambda e: f"Ready for review by {_actor(e)}",
    "convert_to_draft": lambda e: f"Converted to draft by {_actor(e)}",
    "merged": lambda e: f"Merged by {_actor(e)}",
    "closed": lambda e: f"Closed by {_actor(e)}",
    "reopened": lambda e: f"Reopened by {_actor(e)}",
    "head_ref_deleted": lambda e: f"Branch deleted by {_actor(e)}",
    "head_ref_restored": lambda e: f"Branch restored by {_actor(e)}",
    "head_ref_force_pushed": lambda e: f"Force-pushed by {_actor(e)}",
    "base_ref_changed": lambda e: f"Base branch changed by {_actor(e)}",
    "base_ref_force_pushed": lambda e: f"Base branch force-pushed by {_actor(e)}",
    # labels / assignment / metadata
    "labeled": lambda e: f"Labeled {(e.get('label') or {}).get('name', '?')} by {_actor(e)}",
    "unlabeled": lambda e: f"Unlabeled {(e.get('label') or {}).get('name', '?')} by {_actor(e)}",
    "assigned": lambda e: (f"Assigned to {_login(e.get('assignee'))}"
                           + ("" if _login(e.get("assignee")) == _actor(e) else f" by {_actor(e)}")),
    "unassigned": lambda e: f"Unassigned {_login(e.get('assignee'))} by {_actor(e)}",
    "renamed": lambda e: f"Renamed by {_actor(e)}",
    "milestoned": lambda e: f"Milestoned by {_actor(e)}",
    "demilestoned": lambda e: f"Demilestoned by {_actor(e)}",
    "pinned": lambda e: f"Pinned by {_actor(e)}",
    "unpinned": lambda e: f"Unpinned by {_actor(e)}",
    "locked": lambda e: f"Locked by {_actor(e)}",
    "unlocked": lambda e: f"Unlocked by {_actor(e)}",
    # cross-references / linking / hierarchy
    "mentioned": lambda e: f"Mentioned by {_actor(e)}",
    "referenced": lambda e: f"Referenced in commit by {_actor(e)}",
    "cross-referenced": lambda e: f"Cross-referenced by {_actor(e)}",
    "connected": lambda e: f"Connected by {_actor(e)}",
    "disconnected": lambda e: f"Disconnected by {_actor(e)}",
    "parent_issue_added": lambda e: f"Parent issue added by {_actor(e)}",
    "parent_issue_removed": lambda e: f"Parent issue removed by {_actor(e)}",
    "sub_issue_added": lambda e: f"Sub-issue added by {_actor(e)}",
    "sub_issue_removed": lambda e: f"Sub-issue removed by {_actor(e)}",
    # projects (classic + v2)
    "added_to_project": lambda e: f"Added to project by {_actor(e)}",
    "removed_from_project": lambda e: f"Removed from project by {_actor(e)}",
    "moved_columns_in_project": lambda e: f"Moved columns in project by {_actor(e)}",
    "added_to_project_v2": lambda e: f"Added to project by {_actor(e)}",
    "removed_from_project_v2": lambda e: f"Removed from project by {_actor(e)}",
    "project_v2_item_status_changed": lambda e: f"Project status changed by {_actor(e)}",
    "converted_note_to_issue": lambda e: f"Converted note to issue by {_actor(e)}",
    # subscriptions / comments / deploys
    "subscribed": lambda e: f"Subscribed by {_actor(e)}",
    "unsubscribed": lambda e: f"Unsubscribed by {_actor(e)}",
    "comment_deleted": lambda e: f"Comment deleted by {_actor(e)}",
    "deployed": lambda e: f"Deployed by {_actor(e)}",
    "deployment_environment_changed": lambda e: f"Deployment env changed by {_actor(e)}",
}

REVIEW_FMT = {
    "APPROVED": lambda r: f"Approved by {_login(r.get('user'))}",
    "CHANGES_REQUESTED": lambda r: f"Changes requested by {_login(r.get('user'))}",
    "COMMENTED": lambda r: f"Review comment by {_login(r.get('user'))}",
}


def fmt_event(e: dict) -> str:
    fn = EVENT_FMT.get(e.get("event"))
    return fn(e) if fn else f"{e.get('event', 'event')} by {_actor(e)}"


def fmt_review(r: dict) -> str:
    fn = REVIEW_FMT.get(r.get("state"))
    return fn(r) if fn else f"Review {r.get('state', '?')} by {_login(r.get('user'))}"


# Match our own label line: "Labeled <name> by <actor>".
# Non-greedy name, greedy actor — label names ("build-out", "storefront") don't contain " by ".
_LABEL_RE = re.compile(r"^(Labeled|Unlabeled) (.+?) by (.+)$")

# Match our own review-request line: "Review requested from <login> by <actor>".
# Non-greedy reviewer (a single login, no " by "), greedy actor — mirrors _LABEL_RE.
# "Review requested" won't match "Review request removed", so removals stay separate.
_REVIEW_REQ_RE = re.compile(r"^Review requested from (.+?) by (.+)$")


def collapse(acts: list[str]) -> list[str]:
    """Reduce a flat list of formatted event lines, preserving first-seen order:
    1a. merge same-verb + same-actor label lines into one comma-joined line
        ("Labeled build-out by X" + "Labeled storefront by X" → "Labeled build-out, storefront by X");
    1b. merge same-requester review-request lines into one space-joined line
        ("Review requested from john by X" + "...from sara by X" → "...from john sara by X");
    2.  collapse exact-duplicate lines to one, appending " (×N)" when a line occurred N>1 times.
    """
    # 1. consolidate labels by (verb, actor) and review-requests by requester, holding
    #    each group's output slot at first sight so order is preserved.
    merged: list[str | None] = []
    label_groups: dict[tuple[str, str], list[str]] = {}
    label_slot: dict[tuple[str, str], int] = {}
    rr_groups: dict[str, list[str]] = {}      # requester -> reviewers
    rr_slot: dict[str, int] = {}
    for line in acts:
        m = _LABEL_RE.match(line)
        if m:
            key = (m.group(1), m.group(3))  # (verb, actor)
            if key not in label_groups:
                label_slot[key] = len(merged)
                merged.append(None)
                label_groups[key] = []
            if m.group(2) not in label_groups[key]:
                label_groups[key].append(m.group(2))
            continue
        rr = _REVIEW_REQ_RE.match(line)
        if rr:
            requester = rr.group(2)
            if requester not in rr_groups:
                rr_slot[requester] = len(merged)
                merged.append(None)
                rr_groups[requester] = []
            if rr.group(1) not in rr_groups[requester]:
                rr_groups[requester].append(rr.group(1))
            continue
        merged.append(line)
    for key, names in label_groups.items():
        verb, actor = key
        merged[label_slot[key]] = f"{verb} {', '.join(names)} by {actor}"
    for requester, reviewers in rr_groups.items():
        merged[rr_slot[requester]] = f"Review requested from {' '.join(reviewers)} by {requester}"

    # 2. collapse exact duplicates with a count suffix
    counts: dict[str, int] = {}
    order: list[str] = []
    for line in merged:
        if line is None:
            continue
        if line not in counts:
            counts[line] = 0
            order.append(line)
        counts[line] += 1
    return [f"{line} (×{counts[line]})" if counts[line] > 1 else line for line in order]


# ── Issue-activity model (GraphQL) ────────────────────────────────────────────
# One GraphQL call per repo yields issues + author/assignees/labels + timeline
# (commits, comments, links, renames) and — when the env opts into a board — the
# current Status column plus its transitions. Sub-bullets show *movement*; static
# facts (labels, assignees, column) ride the parent row / group header.
_WORK_KINDS = {"commit", "comment"}  # presence ⇒ an "active" issue (board-off grouping)

# Default board-column display order. Unknown columns sort after these (alphabetical);
# "No status" always lands last. Override per-env with board.columns in config.
_COLUMN_ORDER = ["Triage", "Backlog", "Ready", "Todo", "To do", "In progress",
                 "In review", "Blocked", "Done"]


def _dedup(seq) -> list:
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def issue_query(owner: str, name: str, since: str, board: bool) -> str:
    """Build the single issues query. projectItems + PROJECT_V2 status events need a
    Projects:read token, so they're requested ONLY when board is on — board-off runs
    need no extra scope."""
    project_field = (
        'projectItems(first:5){nodes{project{id}'
        'fieldValueByName(name:"Status"){... on ProjectV2ItemFieldSingleSelectValue{name}}}}'
        if board else "")
    status_type = "PROJECT_V2_ITEM_STATUS_CHANGED_EVENT, " if board else ""
    return f'''query {{
  repository(owner: "{owner}", name: "{name}") {{
    issues(first: 30, filterBy: {{since: "{since}"}}, orderBy: {{field: UPDATED_AT, direction: DESC}}) {{
      nodes {{
        number title url createdAt
        author {{ login }}
        assignees(first: 10) {{ nodes {{ login }} }}
        labels(first: 10) {{ nodes {{ name }} }}
        {project_field}
        timelineItems(since: "{since}", last: 60, itemTypes: [{status_type}REFERENCED_EVENT, CROSS_REFERENCED_EVENT, ISSUE_COMMENT, RENAMED_TITLE_EVENT, CONNECTED_EVENT, DISCONNECTED_EVENT]) {{
          nodes {{
            __typename
            ... on ProjectV2ItemStatusChangedEvent {{ previousStatus status createdAt }}
            ... on ReferencedEvent {{ actor {{ login }} }}
            ... on CrossReferencedEvent {{ actor {{ login }} }}
            ... on IssueComment {{ author {{ login }} }}
            ... on ConnectedEvent {{ subject {{ __typename ... on PullRequest {{ number url }} ... on Issue {{ number url }} }} }}
            ... on DisconnectedEvent {{ subject {{ __typename ... on PullRequest {{ number url }} ... on Issue {{ number url }} }} }}
          }}
        }}
      }}
    }}
  }}
}}'''


def normalize_issue(node: dict, board: bool) -> dict:
    """Flatten a GraphQL issue node to the shape the renderer consumes."""
    events, moves = [], []
    for t in ((node.get("timelineItems") or {}).get("nodes") or []):
        tn = t.get("__typename")
        if tn == "ProjectV2ItemStatusChangedEvent":
            moves.append({"prev": t.get("previousStatus"), "to": t.get("status"),
                          "at": t.get("createdAt") or ""})
        elif tn in ("ReferencedEvent", "CrossReferencedEvent"):
            actor = _login(t.get("actor"))
            if not _is_bot(actor):  # hide automated commit cross-refs
                events.append({"kind": "commit", "actor": actor})
        elif tn == "IssueComment":
            actor = _login(t.get("author"))
            if not _is_bot(actor):  # hide bot comments
                events.append({"kind": "comment", "actor": actor})
        elif tn == "RenamedTitleEvent":
            events.append({"kind": "rename"})
        elif tn in ("ConnectedEvent", "DisconnectedEvent"):
            subj = t.get("subject") or {}
            events.append({"kind": "link",
                           "to_type": subj.get("__typename"),
                           "number": subj.get("number"),
                           "url": subj.get("url")})
    project_items = []
    if board:
        for pi in ((node.get("projectItems") or {}).get("nodes") or []):
            proj = (pi or {}).get("project") or {}
            fv = (pi or {}).get("fieldValueByName") or {}
            project_items.append({"pid": proj.get("id"), "status": fv.get("name")})
    return {
        "number": node["number"], "title": node["title"], "url": node["url"],
        "createdAt": node.get("createdAt") or "",
        "reporter": _login(node.get("author")),
        "assignees": _dedup(_login(a) for a in ((node.get("assignees") or {}).get("nodes") or [])),
        "labels": [(lbl or {}).get("name", "?") for lbl in ((node.get("labels") or {}).get("nodes") or [])],
        "status": None,            # resolved against the pinned board in main()
        "project_items": project_items,
        "events": events,
        "moves": moves,
    }


def issue_parent_row(it: dict, new: bool = False) -> str:
    """One line: [NEW] link + title + assignee(s) (else creator) + 🏷️ label badges."""
    # Show who's on the hook: assignee(s) if anyone is assigned, else fall back to the
    # creator. Showing both ("creator → assignee") was noise; the assignee is who matters.
    assignees = [a for a in it["assignees"] if a != it["reporter"]]
    who = ", ".join(assignees) if assignees else it["reporter"]
    badge = f" 🏷️ {', '.join(it['labels'])}" if it["labels"] else ""
    tag = "NEW " if new else ""
    return f"• {tag}<{it['url']}|#{it['number']}> {it['title']} {who}{badge}"


def issue_movement(it: dict) -> list[str]:
    """Movement sub-bullets: status journey (only when columns actually changed),
    commits (per-author counts), comments (distinct people), links, renames."""
    subs: list[str] = []
    moves = sorted(it["moves"], key=lambda m: m.get("at") or "")
    if moves:
        prev0, last = moves[0].get("prev"), moves[-1].get("to")
        if prev0 and last and prev0 != last:           # genuinely moved columns
            subs.append(f"{prev0} → {last}")
        elif prev0 and prev0 == last and len(moves) > 1:  # bounced out and back
            subs.append(f"churned within {last} (×{len(moves)})")
        # brand-new (∅ → column): suppressed — the column group + NEW tag already say it
    commits = [e for e in it["events"] if e["kind"] == "commit"]
    if commits:
        per: dict[str, int] = {}
        for e in commits:
            per[e["actor"]] = per.get(e["actor"], 0) + 1
        # show "N×actor" only when N>1; a single commit is just the actor.
        who = ", ".join(f"{c}×{a}" if c > 1 else a
                        for a, c in sorted(per.items(), key=lambda kv: -kv[1]))
        subs.append(f"commit{'s' if len(commits) != 1 else ''} by {who}")
    comments = [e for e in it["events"] if e["kind"] == "comment"]
    if comments:
        subs.append(f"comment{'s' if len(comments) != 1 else ''} by "
                    f"{', '.join(_dedup(e['actor'] for e in comments))}")
    # Linked PRs/issues: name the target ("Linked to PR <#123>") instead of a bare "Linked".
    seen_links: set[str] = set()
    for e in (e for e in it["events"] if e["kind"] == "link"):
        num, url = e.get("number"), e.get("url")
        if url and num and url not in seen_links:
            seen_links.add(url)
            kind = "PR" if e.get("to_type") == "PullRequest" else "issue"
            subs.append(f"Linked to {kind} <{url}|#{num}>")
        elif not (url and num) and "Linked" not in seen_links:
            seen_links.add("Linked")          # subject unavailable — fall back to bare verb
            subs.append("Linked")
    if any(e["kind"] == "rename" for e in it["events"]):
        subs.append("Renamed")
    return subs


def render_issue(it: dict, emit, new: bool = False) -> None:
    """Emit one issue: parent row (indent 2) + movement sub-bullets (indent 6).
    Shared by the board-column and Active/New groupings so the row/indent contract
    lives in one place."""
    emit(issue_parent_row(it, new=new), indent=2)
    for line in issue_movement(it):
        emit(line, indent=6)


def board_column_order(board_id: str, token: str) -> list | None:
    """Live column order for the pinned board — fetched each run, since boards get
    reordered. Owner-agnostic via node(id:). Returns None on any error so callers fall
    back to the default order rather than failing."""
    query = (f'query {{ node(id: "{board_id}") {{ ... on ProjectV2 {{ '
             f'field(name: "Status") {{ ... on ProjectV2SingleSelectField {{ '
             f'options {{ name }} }} }} }} }} }}')
    try:
        data = gh_graphql(query, token)
    except GhError:
        return None
    opts = ((((data.get("data") or {}).get("node") or {}).get("field") or {}).get("options")) or []
    return [o.get("name") for o in opts] or None


def column_sort_key(col: str, custom: list | None):
    order = custom or _COLUMN_ORDER
    if col == "No status":
        return (2, 0, col)            # always last
    if col in order:
        return (0, order.index(col), col)
    return (1, 0, col)                # unknown columns: after known, alphabetical


# Slack mrkdwn link: <url|display text>. We emojify display text and plain prose but
# NEVER the url — a roster key that coincides with a URL path segment (a login that is
# also an org/repo name) would otherwise be substituted inside the href and break the link.
_LINK_RE = re.compile(r"<([^|>]+)\|([^>]*)>")


def make_emojify(roster: dict):
    if not roster:
        return lambda s: s
    keys = sorted(roster, key=len, reverse=True)  # longest first: NinadMaladkar > Ninad
    pat = re.compile(r"\b(" + "|".join(re.escape(k) for k in keys) + r")\b")
    sub = lambda s: pat.sub(lambda m: roster[m.group(0)], s)

    def emojify(s: str) -> str:
        out, pos = [], 0
        for m in _LINK_RE.finditer(s):
            out.append(sub(s[pos:m.start()]))                # prose before the link
            out.append(f"<{m.group(1)}|{sub(m.group(2))}>")  # url verbatim, text emojified
            pos = m.end()
        out.append(sub(s[pos:]))                             # trailing prose
        return "".join(out)

    return emojify


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
    try:
        window = int(os.environ.get("SHIPIT_WINDOW_HOURS", cfg.get("window_hours", 24)))
        if not 0 < window <= 24 * 365 * 10:  # bound: avoid a malformed env var → unbounded API pull
            window = 24
    except (TypeError, ValueError):
        window = 24
    since = (datetime.now(timezone.utc) - timedelta(hours=window)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Board: pinned by node id at the env level (one board per org, shared across the
    # env's repos). Its column order is fetched live once per run — boards get reordered.
    board_id = envs[env_type].get("board")
    board = bool(board_id)
    board_columns = board_column_order(board_id, token) if isinstance(board_id, str) else None

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
                emit(f"\n*Merged PRs ({len(merged)}):* ✅")
                for pr in merged:
                    emit(f"  • <{pr['url']}|#{pr['number']}> {pr['title']} "
                         f"{_short(pr.get('author'))}/{_short(pr.get('mergedBy'))}")
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
            emit(f"\n*Currently Open PRs ({len(open_prs)}):* ⏳")
            for pr in open_prs:
                tag = "NEW: " if pr["createdAt"] >= since else ""
                emit(f"  • <{pr['url']}|#{pr['number']}> {tag}{pr['title']} {_short(pr.get('author'))}")
                if pr["updatedAt"] < since:
                    continue
                acts: list[str] = []
                try:
                    for e in gh_json(["api", f"repos/{repo}/issues/{pr['number']}/events"], token):
                        if e.get("created_at", "") >= since and not _is_bot(_actor(e)):
                            acts.append(fmt_event(e))
                    for c in gh_json(["api", f"repos/{repo}/issues/{pr['number']}/comments"], token):
                        if c.get("created_at", "") >= since and not _is_bot(_login(c.get("user"))):
                            acts.append(f"Comment by {_login(c.get('user'))}")
                    for r in gh_json(["api", f"repos/{repo}/pulls/{pr['number']}/reviews"], token):
                        if (r.get("submitted_at") or "") >= since and not _is_bot(_login(r.get("user"))):
                            acts.append(fmt_review(r))
                except GhError as exc:
                    emit(f"      ⚠️  activity fetch failed: {exc}")
                for line in collapse(acts):  # merge labels, collapse exact dups (×N)
                    emit(line, indent=6)
            activity = True

        # Issue activity — ONE GraphQL call per repo (issues + timeline [+ board status]).
        # The whole section degrades to an inline note: a fetch error OR a malformed node
        # must never abort the digest (the PR sections above honor the same contract).
        owner, _, name = repo.partition("/")
        try:
            data = gh_graphql(issue_query(owner, name, since, board), token)
            nodes = ((((data.get("data") or {}).get("repository") or {})
                      .get("issues") or {}).get("nodes")) or []
            issues = [normalize_issue(n, board) for n in nodes if n]
            if board:
                # Resolve each issue's column from the pinned board (one board per org);
                # an issue not on that board → No status. Falls back to first project when
                # board is enabled without a pinned id.
                for it in issues:
                    items = it["project_items"]
                    if isinstance(board_id, str):
                        it["status"] = next((pi["status"] for pi in items if pi["pid"] == board_id), None)
                    elif items:
                        it["status"] = items[0]["status"]
            if issues:
                emit("\n*Issue Activity* 📝")
                if board:
                    # Group by current board column; NEW prefixes issues created in-window.
                    groups: dict[str, list] = {}
                    for it in issues:
                        groups.setdefault(it["status"] or "No status", []).append(it)
                    for col in sorted(groups, key=lambda c: column_sort_key(c, board_columns)):
                        emit(f"\n  *{col} ({len(groups[col])}):* 📋")
                        for it in groups[col]:
                            render_issue(it, emit, new=it["createdAt"] >= since)
                else:
                    # No board: "active" (has commit/comment work) vs "new"/triage.
                    active, new = [], []
                    for it in issues:
                        (active if any(e["kind"] in _WORK_KINDS for e in it["events"]) else new).append(it)
                    for label, bucket in (("Active", active), ("New", new)):
                        if not bucket:
                            continue
                        emit(f"\n  *{label} ({len(bucket)}):*")
                        for it in bucket:
                            render_issue(it, emit)
                activity = True
        except GhError as exc:
            emit(f"  ⚠️  issue fetch failed: {exc}")
        except Exception as exc:  # malformed node / unexpected shape — degrade, never abort
            emit(f"  ⚠️  issue render failed: {exc}")

    if not activity:
        emit(f"\n(No activity in the last {window}h)")

    print("\n".join(lines))
    return 0  # ALWAYS 0: a digest must never deliver as an error alert.


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise  # preserve main()'s deliberate exit codes (e.g. return 2 for unknown env)
    except BaseException as exc:  # noqa: BLE001 — a cron digest must never exit non-zero
        print(f"ship-it-digest: unexpected error: {exc}", file=sys.stderr)
        sys.exit(0)
