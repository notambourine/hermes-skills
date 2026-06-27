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

The look-back window is counted in *business hours* (Mon–Fri, UTC): weekend hours are
free, so on a Monday a 24-hour window reaches back to Friday rather than stopping at a
dead Sunday. Mid-week it behaves exactly like a flat hour count. See business_hours_ago.

Internally the digest is built functionally: each section returns a list of raw (already
indented, pre-emojify) lines plus an `active` flag, render_digest concatenates them and
emojifies the body in one final pass (the header is left raw). The normalized issue data
is modelled as @dataclasses (Issue/IssueEvent/Move/...) so the renderer reads fields by
attribute, while raw `gh` JSON stays dict at the API boundary.

Usage:  ship_it_digest.py [ENV_TYPE]        # env may also come from $ENV_TYPE
Env:    ENV_TYPE              which environment row to run (if not passed as arg)
        SHIPIT_WINDOW_HOURS   look-back window in *business* hours (overrides config; default 24)
        <token env>           per-env token, named by config; falls back to TARGET_GH_TOKEN
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent


class GhError(RuntimeError):
  """A `gh` invocation failed; surfaced inline, never fatal."""


# ── Data model (internal, post-parse) ─────────────────────────────────────────
# External `gh` JSON (PR-list rows, REST event dicts) stays dict — that's the API
# boundary. Only the *normalized* issue shape and per-run config are typed, so the
# renderer reads fields by attribute instead of stringly-keyed dict lookups.
@dataclass
class Move:
  """One board-column transition on an issue's timeline."""
  prev: str | None
  to: str | None
  at: str


@dataclass
class IssueEvent:
  """A timeline event, tagged by kind ∈ {commit, comment, rename, link}."""
  kind: str
  actor: str | None = None
  to_type: str | None = None
  number: int | None = None
  url: str | None = None


@dataclass
class ProjectItem:
  """An issue's membership in one ProjectV2 board, carrying its Status column."""
  pid: str | None
  status: str | None


@dataclass
class Issue:
  """A normalized issue: static facts (title/labels/assignees) + movement, ready to render."""
  number: int
  title: str
  url: str
  created_at: str
  reporter: str
  assignees: list[str]
  labels: list[str]
  events: list[IssueEvent]
  moves: list[Move]
  project_items: list[ProjectItem]
  status: str | None = None  # resolved against the pinned board in board_groups


@dataclass
class IssueGroup:
  """A rendered issue section: a heading + its issues, each flagged for a NEW prefix."""
  title: str
  emoji: str
  items: list[tuple[Issue, bool]]


@dataclass
class RunContext:
  """Everything one digest run needs, resolved once from config + environment."""
  env_type: str
  repos: list[str]
  token: str
  since: str
  window: int
  board_id: str | None
  board: bool
  board_columns: list[str] | None
  emojify: Callable[[str], str]


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


# GitHub Apps whose login GraphQL surfaces WITHOUT the REST "[bot]" suffix. The inline
# `gh pr list` reviews/comments payload (GraphQL-backed) returns "railway-app", not
# "railway-app[bot]", so the suffix check alone can't catch them — these universal app
# logins (same string in every repo) close that gap.
_BOT_LOGINS = frozenset({
    "railway-app", "github-actions", "socket-security", "dependabot",
    "vercel", "netlify", "codecov", "coderabbitai", "sentry-io",
})


def _is_bot(login: str) -> bool:
  """Hide bot-authored timeline noise — automated deploys, CI, dependency bumps — from
    the digest, which is about human activity. REST renders bots as 'name[bot]'; the
    GraphQL-backed pr-list path drops the suffix, so we also match _BOT_LOGINS."""
  return login.endswith("[bot]") or login in _BOT_LOGINS


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

def fmt_event(e: dict) -> str:
  fn = EVENT_FMT.get(e.get("event"))
  return fn(e) if fn else f"{e.get('event', 'event')} by {_actor(e)}"


# Reviews and comments fold into ONE compact bracket appended to the parent row, grouped by
# activity: "[👍 A B · 🔧 C · 👀 D · 🗨️ E]" (people are roster logins → custom emoji). This
# keeps a busy PR on a single line instead of one sub-line per kind. Inputs are pre-filtered
# to the window and bot-free, normalized to {"state","login"} reviews and {"login"} comments
# so the merged (gh pr list inline) and open (per-PR API) paths share one renderer.
_ACTIVITY_GLYPH = {"APPROVED": "👍", "CHANGES_REQUESTED": "🔧", "COMMENTED": "👀"}


def review_comment_cluster(reviews: list[dict], comments: list[dict]) -> str:
  """A leading-space bracket folding all by-person rollups onto the parent row, grouped
    by activity (glyph + space-joined people, kinds joined by ' · '). '' when empty, so
    callers append it unconditionally."""
  parts: list[str] = []
  by_state: dict[str, list[str]] = {}
  for r in reviews:
    by_state.setdefault(r.get("state") or "", []).append(r.get("login") or "unknown")
  for state in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED"):
    if state in by_state:
      parts.append(f"{_ACTIVITY_GLYPH[state]} {' '.join(_dedup(by_state[state]))}")
  if comments:
    people = _dedup(c.get("login") or "unknown" for c in comments)
    parts.append(f"🗨️ {' '.join(people)}")
  return f"  [{' · '.join(parts)}]" if parts else ""


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


def parse_issue(node: dict, board: bool) -> Issue:
  """Flatten a GraphQL issue node into a typed Issue. The __typename if/elif chain is the
    edge parser: it reads raw dicts and emits typed IssueEvent/Move values."""
  events: list[IssueEvent] = []
  moves: list[Move] = []
  for t in ((node.get("timelineItems") or {}).get("nodes") or []):
    tn = t.get("__typename")
    if tn == "ProjectV2ItemStatusChangedEvent":
      moves.append(Move(prev=t.get("previousStatus"), to=t.get("status"),
                        at=t.get("createdAt") or ""))
    elif tn in ("ReferencedEvent", "CrossReferencedEvent"):
      actor = _login(t.get("actor"))
      if not _is_bot(actor):  # hide automated commit cross-refs
        events.append(IssueEvent(kind="commit", actor=actor))
    elif tn == "IssueComment":
      actor = _login(t.get("author"))
      if not _is_bot(actor):  # hide bot comments
        events.append(IssueEvent(kind="comment", actor=actor))
    elif tn == "RenamedTitleEvent":
      events.append(IssueEvent(kind="rename"))
    elif tn in ("ConnectedEvent", "DisconnectedEvent"):
      subj = t.get("subject") or {}
      events.append(IssueEvent(kind="link", to_type=subj.get("__typename"),
                               number=subj.get("number"), url=subj.get("url")))
  project_items: list[ProjectItem] = []
  if board:
    for pi in ((node.get("projectItems") or {}).get("nodes") or []):
      proj = (pi or {}).get("project") or {}
      fv = (pi or {}).get("fieldValueByName") or {}
      project_items.append(ProjectItem(pid=proj.get("id"), status=fv.get("name")))
  return Issue(
      number=node["number"], title=node["title"], url=node["url"],
      created_at=node.get("createdAt") or "",
      reporter=_login(node.get("author")),
      assignees=_dedup(_login(a) for a in ((node.get("assignees") or {}).get("nodes") or [])),
      labels=[(lbl or {}).get("name", "?") for lbl in ((node.get("labels") or {}).get("nodes") or [])],
      events=events,
      moves=moves,
      project_items=project_items,
  )


def issue_parent_row(it: Issue, new: bool = False) -> str:
  """One line: link + NEW + title + assignee(s) (else creator) + [label] badges.
    NEW rides *after* the link (mirrors the open-PR row); labels are bracketed, not
    emoji-prefixed."""
  # Show who's on the hook: assignee(s) if anyone is assigned, else fall back to the
  # creator. Showing both ("creator → assignee") was noise; the assignee is who matters.
  assignees = [a for a in it.assignees if a != it.reporter]
  who = ", ".join(assignees) if assignees else it.reporter
  badge = f" [{', '.join(it.labels)}]" if it.labels else ""
  tag = "NEW " if new else ""
  return f"• <{it.url}|#{it.number}> {tag}{it.title} {who}{badge}"


def issue_movement(it: Issue) -> list[str]:
  """Movement sub-bullets: status journey (only when columns actually changed),
    commits (per-author counts), comments (distinct people), links, renames."""
  subs: list[str] = []
  moves = sorted(it.moves, key=lambda m: m.at or "")
  if moves:
    prev0, last = moves[0].prev, moves[-1].to
    if prev0 and last and prev0 != last:           # genuinely moved columns
      subs.append(f"{prev0} → {last}")
    elif prev0 and prev0 == last and len(moves) > 1:  # bounced out and back
      subs.append(f"churned within {last} (×{len(moves)})")
      # brand-new (∅ → column): suppressed — the column group + NEW tag already say it
  commits = [e for e in it.events if e.kind == "commit"]
  if commits:
    per: dict[str, int] = {}
    for e in commits:
      per[e.actor] = per.get(e.actor, 0) + 1
    # show "N×actor" only when N>1; a single commit is just the actor.
    who = ", ".join(f"{c}×{a}" if c > 1 else a
                    for a, c in sorted(per.items(), key=lambda kv: -kv[1]))
    subs.append(f"commit{'s' if len(commits) != 1 else ''} by {who}")
  comments = [e for e in it.events if e.kind == "comment"]
  if comments:
    subs.append(f"comment{'s' if len(comments) != 1 else ''} by "
                f"{', '.join(_dedup(e.actor for e in comments))}")
  # Linked PRs/issues: name the target ("Linked to PR <#123>") instead of a bare "Linked".
  seen_links: set[str] = set()
  for e in (e for e in it.events if e.kind == "link"):
    num, url = e.number, e.url
    if url and num and url not in seen_links:
      seen_links.add(url)
      kind = "PR" if e.to_type == "PullRequest" else "issue"
      subs.append(f"Linked to {kind} <{url}|#{num}>")
    elif not (url and num) and "Linked" not in seen_links:
      seen_links.add("Linked")          # subject unavailable — fall back to bare verb
      subs.append("Linked")
  if any(e.kind == "rename" for e in it.events):
    subs.append("Renamed")
  return subs


def _indent(lines: list[str], n: int) -> list[str]:
  """Prepend n spaces to each line — sub-bullet indentation. Done BEFORE the final
    emojify pass; byte-identical because emojify matches roster keys on \\b boundaries,
    so leading spaces never alter a substitution."""
  pad = " " * n
  return [pad + line for line in lines]


def render_issue_lines(it: Issue, new: bool = False) -> list[str]:
  """One issue as raw lines: parent row (indent 2) + movement sub-bullets (indent 6).
    Shared by the board-column and Active/New groupings so the row/indent contract lives
    in one place."""
  return _indent([issue_parent_row(it, new=new)], 2) + _indent(issue_movement(it), 6)


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
  def sub(s):
    return pat.sub(lambda m: roster[m.group(0)], s)

  def emojify(s: str) -> str:
    out, pos = [], 0
    for m in _LINK_RE.finditer(s):
      out.append(sub(s[pos:m.start()]))                # prose before the link
      out.append(f"<{m.group(1)}|{sub(m.group(2))}>")  # url verbatim, text emojified
      pos = m.end()
    out.append(sub(s[pos:]))                             # trailing prose
    return "".join(out)

  return emojify


def section_title(text: str, emoji: str = "") -> str:
  """A section heading line: emoji at the *front* (not trailing), title in
    bold-italic (`_**…**_`). Keeps the leading blank line that spaces sections apart."""
  prefix = f"{emoji} " if emoji else ""
  return f"\n{prefix}_**{text}**_"


# ── Look-back window ──────────────────────────────────────────────────────────
# KEY-DECISION 2026-06-26: the window is measured in *business hours*, not wall-clock
# hours. The cron runs weekday mornings ('0 13 * * 1-5'); a flat 24h look-back on a
# MONDAY reaches back only to a dead Sunday and misses all of Friday's shipping. Counting
# only Mon–Fri hours makes `since` land at the same clock time on the prior *business*
# day — yesterday on Tue–Fri, Friday on Monday. Weekday is judged in UTC, matching the
# engine's UTC timestamps and the UTC cron, and weekend determination only ever pushes
# `since` further back (more inclusive), so no activity is dropped by the choice.
def business_hours_ago(now: datetime, hours: int) -> datetime:
  """Walk back `hours` business hours from `now`, skipping weekend days entirely.
    Each one-hour block counts only if its start lands on a weekday (Mon=0..Fri=4);
    Sat/Sun hours are free. Mid-week there's no weekend in the last `hours`, so this is
    identical to `now - timedelta(hours=hours)`."""
  cursor = now
  remaining = hours
  while remaining > 0:
    prev = cursor - timedelta(hours=1)
    if prev.weekday() < 5:        # Mon–Fri consume the budget; Sat/Sun are free
      remaining -= 1
    cursor = prev
  return cursor


# ── Sections (each returns raw, already-indented lines + an `active` flag) ─────-
# A section's `active` flag mirrors the old `activity = True` placement exactly: set ONLY
# when the section has real content (merged PRs / open PRs / issues), never for a
# warning-only result — so a repo that only emits ⚠️ notes still falls through to the
# "(No activity…)" footer.
SectionResult = tuple[list[str], bool]


def merged_pr_line(pr: dict, since: str) -> str:
  """One merged-PR row: link + title + author + finishers + review/comment cluster."""
  # KEY-DECISION 2026-06-26: the header's second slot is the set of people who *finished*
  # the PR — anyone who approved (in-window) plus whoever clicked merge — minus the
  # author, deduped, space-joined. Self-merges with no external approval render
  # author-only (no " · "), which reads as "no second human touched it". The approved
  # sub-line is dropped (its names now live here); changes-requested/comments still show.
  author_login = _login(pr.get("author"))
  finishers = [r.get("author") for r in (pr.get("reviews") or [])
               if r.get("state") == "APPROVED"
               and (r.get("submittedAt") or "") >= since
               and not _is_bot(_login(r.get("author")))]
  finishers.append(pr.get("mergedBy"))
  seen, names = set(), []
  for u in finishers:
    login = _login(u)
    if login in ("unknown", author_login) or login in seen or _is_bot(login):
      continue
    seen.add(login)
    names.append(_short(u))
  second = f" · {' '.join(names)}" if names else ""
  # Reviews/comments ride inline on the pr-list payload (no per-PR call); filter to the
  # window + drop bots, then fold into the parent-row bracket. APPROVED is excluded here —
  # those names already ride the finishers slot.
  reviews = [{"state": r.get("state"), "login": _login(r.get("author"))}
             for r in (pr.get("reviews") or [])
             if (r.get("submittedAt") or "") >= since
             and not _is_bot(_login(r.get("author")))
             and r.get("state") != "APPROVED"]
  comments = [{"login": _login(c.get("author"))}
              for c in (pr.get("comments") or [])
              if (c.get("createdAt") or "") >= since
              and not _is_bot(_login(c.get("author")))]
  return (f"  • <{pr['url']}|#{pr['number']}> {pr['title']} "
          f"{_short(pr.get('author'))}{second}"
          f"{review_comment_cluster(reviews, comments)}")


def open_pr_lines(pr: dict, repo: str, since: str, token: str) -> list[str]:
  """One open-PR row + collapsed lifecycle sub-bullets. Lifecycle events go through
    collapse() (labels merge, ×N dedup) as sub-bullets; reviews and comments fold into a
    bracket on the parent row. The per-PR timeline is fetched only for PRs updated
    in-window — a stale PR shows just its row — and a fetch error degrades to a sub-bullet
    under the row (never abort, never reorder)."""
  tag = "NEW: " if pr["createdAt"] >= since else ""
  events: list[str] = []
  comments: list[dict] = []
  reviews: list[dict] = []
  connected_by: list[str] = []  # actors who linked an issue, in-window
  if pr["updatedAt"] >= since:
    try:
      for e in gh_json(["api", f"repos/{repo}/issues/{pr['number']}/events"], token):
        if e.get("created_at", "") >= since and not _is_bot(_actor(e)):
          # The REST `connected` event carries no target; name the linked issue(s)
          # from the PR's closingIssuesReferences instead.
          if e.get("event") == "connected":
            connected_by.append(_actor(e))
          else:
            events.append(fmt_event(e))
      refs = pr.get("closingIssuesReferences") or []
      if connected_by:
        by = ", ".join(_dedup(connected_by))
        if refs:
          events.extend(f"Connected to <{r['url']}|#{r['number']}> by {by}"
                        for r in refs)
        else:
          events.append(f"Connected by {by}")
      for c in gh_json(["api", f"repos/{repo}/issues/{pr['number']}/comments"], token):
        if c.get("created_at", "") >= since and not _is_bot(_login(c.get("user"))):
          comments.append({"login": _login(c.get("user"))})
      for r in gh_json(["api", f"repos/{repo}/pulls/{pr['number']}/reviews"], token):
        if (r.get("submitted_at") or "") >= since and not _is_bot(_login(r.get("user"))):
          reviews.append({"state": r.get("state"), "login": _login(r.get("user"))})
    except GhError as exc:
      events.append(f"⚠️  activity fetch failed: {exc}")
  row = (f"  • <{pr['url']}|#{pr['number']}> {tag}{pr['title']} "
         f"{_short(pr.get('author'))}{review_comment_cluster(reviews, comments)}")
  return [row, *_indent(collapse(events), 6)]  # merge labels, collapse exact dups (×N)


def digest_merged_prs(repo: str, ctx: RunContext) -> SectionResult:
  try:
    merged = gh_json(
        ["pr", "list", "-R", repo, "--state", "merged",
         "--search", f"merged:>={ctx.since}",
         "--json", "number,title,author,url,mergedBy,reviews,comments"], ctx.token)
  except GhError as exc:
    return [f"  ⚠️  merged-PR fetch failed: {exc}"], False
  if not merged:
    return [], False
  lines = [section_title(f"Merged PRs ({len(merged)}):", "✅")]
  lines += [merged_pr_line(pr, ctx.since) for pr in merged]
  return lines, True


def digest_open_prs(repo: str, ctx: RunContext) -> SectionResult:
  try:
    open_prs = gh_json(
        ["pr", "list", "-R", repo, "--state", "open",
         "--json", "number,title,author,url,updatedAt,createdAt,closingIssuesReferences"], ctx.token)
  except GhError as exc:
    return [f"  ⚠️  open-PR fetch failed: {exc}"], False
  if not open_prs:
    return [], False
  lines = [section_title(f"Currently Open PRs ({len(open_prs)}):", "⏳")]
  for pr in open_prs:
    lines += open_pr_lines(pr, repo, ctx.since, ctx.token)
  return lines, True


def board_groups(issues: list[Issue], ctx: RunContext) -> list[IssueGroup]:
  """Board path: resolve each issue's column from the pinned board (one board per org; an
    issue not on it → No status; falls back to the first project when board is enabled
    without a pinned id), group by column, order columns via column_sort_key. Each issue
    is flagged NEW when created in-window."""
  for it in issues:
    items = it.project_items
    if isinstance(ctx.board_id, str):
      it.status = next((pi.status for pi in items if pi.pid == ctx.board_id), None)
    elif items:
      it.status = items[0].status
  grouped: dict[str, list[Issue]] = {}
  for it in issues:
    grouped.setdefault(it.status or "No status", []).append(it)
  out: list[IssueGroup] = []
  for col in sorted(grouped, key=lambda c: column_sort_key(c, ctx.board_columns)):
    items = [(it, it.created_at >= ctx.since) for it in grouped[col]]
    out.append(IssueGroup(title=col, emoji="📋", items=items))
  return out


def birth_groups(issues: list[Issue], since: str) -> list[IssueGroup]:
  """No-board path: split by birth, not by event-kind. "New" = opened in the window;
    "Active" = pre-existing issue with in-window activity (every issue here is already
    updated-since-`since` by the query filter). The NEW prefix is never shown — the group
    heading already says it."""
  active: list[Issue] = []
  new: list[Issue] = []
  for it in issues:
    (new if it.created_at >= since else active).append(it)
  out: list[IssueGroup] = []
  for label, emoji, bucket in (("Active Issues", "🔄", active),
                               ("New Issues", "🆕", new)):
    if not bucket:
      continue
    out.append(IssueGroup(title=label, emoji=emoji, items=[(it, False) for it in bucket]))
  return out


def digest_issues(repo: str, ctx: RunContext) -> SectionResult:
  """Issue activity — ONE GraphQL call per repo (issues + timeline [+ board status]).
    Issue groups are top-level sections (siblings of Merged/Open PRs) — no "Issue Activity"
    wrapper; the board column / Active-New is the heading. The whole section degrades to an
    inline note: a fetch error OR a malformed node must never abort the digest. `lines` is
    declared before the try so a mid-render failure keeps the partial output, matching the
    original emit-as-you-go behavior."""
  owner, _, name = repo.partition("/")
  lines: list[str] = []
  try:
    data = gh_graphql(issue_query(owner, name, ctx.since, ctx.board), ctx.token)
    nodes = ((((data.get("data") or {}).get("repository") or {})
              .get("issues") or {}).get("nodes")) or []
    issues = [parse_issue(n, ctx.board) for n in nodes if n]
    if not issues:
      return [], False
    groups = board_groups(issues, ctx) if ctx.board else birth_groups(issues, ctx.since)
    for g in groups:
      lines.append(section_title(f"{g.title} ({len(g.items)}):", g.emoji))
      for it, new in g.items:
        lines += render_issue_lines(it, new)
    return lines, True
  except GhError as exc:
    lines.append(f"  ⚠️  issue fetch failed: {exc}")
    return lines, False
  except Exception as exc:  # malformed node / unexpected shape — degrade, never abort
    lines.append(f"  ⚠️  issue render failed: {exc}")
    return lines, False


def digest_repo(repo: str, ctx: RunContext) -> SectionResult:
  """One repo block: access check + header + the three sections, concatenated in order
    (merged PRs, open PRs, issues). A repo we can't read degrades to a single ⚠️ line."""
  try:
    run_gh(["repo", "view", repo, "--json", "name"], ctx.token)
  except GhError as exc:
    return [f"\n⚠️  Cannot access repository {repo}: {exc}"], False
  lines = [f"\n--- Repository: {repo} ---"]
  active = False
  for section in (digest_merged_prs, digest_open_prs, digest_issues):
    s_lines, s_active = section(repo, ctx)
    lines += s_lines
    active = active or s_active
  return lines, active


def render_digest(ctx: RunContext) -> list[str]:
  """Assemble the full digest: a raw header line + every repo block, then emojify the body
    in a single final pass."""
  # KEY-DECISION 2026-06-26: the header line is appended RAW and never emojified — a roster
  # key colliding with the env name must not be substituted into the title (the original
  # never ran it through emit). Only body lines go through ctx.emojify; emojify is per-line
  # pure, so one batched pass here is byte-identical to the old emit-per-line.
  header = f"🚢 Daily Ship-It Briefing — {ctx.env_type.upper()} (last {ctx.window} business hrs)"
  body: list[str] = []
  active = False
  for repo in ctx.repos:
    r_lines, r_active = digest_repo(repo, ctx)
    body += r_lines
    active = active or r_active
  if not active:
    body.append(f"\n(No activity in the last {ctx.window} business hrs)")
  return [header, *(ctx.emojify(line) for line in body)]


# ── Engine ──────────────────────────────────────────────────────────────────--
def build_context(cfg: dict, env_type: str) -> RunContext:
  """Resolve one environment row + env overrides into a RunContext: token (per-env name,
    falling back to TARGET_GH_TOKEN), look-back window (bounded), `since`, the pinned board
    + its live column order, and the roster emojifier."""
  env = cfg.get("environments", {})[env_type]
  repos = env.get("repos", [])
  token = (os.environ.get(env.get("token_env", ""), "")
           or os.environ.get("TARGET_GH_TOKEN", ""))
  try:
    window = int(os.environ.get("SHIPIT_WINDOW_HOURS", cfg.get("window_hours", 24)))
    if not 0 < window <= 24 * 365 * 10:  # bound: a malformed env var → unbounded API pull
      window = 24
  except (TypeError, ValueError):
    window = 24
  since = business_hours_ago(datetime.now(timezone.utc), window).strftime("%Y-%m-%dT%H:%M:%SZ")
  # Board: pinned by node id at the env level (one board per org, shared across the env's
  # repos). Its column order is fetched live once per run — boards get reordered.
  board_id = env.get("board")
  board_columns = board_column_order(board_id, token) if isinstance(board_id, str) else None
  return RunContext(
      env_type=env_type, repos=repos, token=token, since=since, window=window,
      board_id=board_id, board=bool(board_id), board_columns=board_columns,
      emojify=make_emojify(cfg.get("roster", {})),
  )


def main() -> int:
  cfg = load_config()
  envs = cfg.get("environments", {})
  env_type = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ENV_TYPE", "")).strip()
  if not env_type or env_type not in envs:
    known = ", ".join(envs) or "none"
    print(f"ship-it-digest: unknown or missing ENV_TYPE '{env_type}'. Known: {known}",
          file=sys.stderr)
    return 2  # config error before any delivery — fine to be non-zero here
  print("\n".join(render_digest(build_context(cfg, env_type))))
  return 0  # ALWAYS 0: a digest must never deliver as an error alert.


if __name__ == "__main__":
  try:
    sys.exit(main())
  except SystemExit:
    raise  # preserve main()'s deliberate exit codes (e.g. return 2 for unknown env)
  except BaseException as exc:  # noqa: BLE001 — a cron digest must never exit non-zero
    print(f"ship-it-digest: unexpected error: {exc}", file=sys.stderr)
    sys.exit(0)
