#!/usr/bin/env bash
# ship-it-digest — daily GitHub activity briefing for a named environment.
#
# Emits Slack mrkdwn (<url|text> links, :custom_emoji:) summarizing the last N hours
# of a repo's activity: merged PRs, open PRs with per-PR timeline events, and issues
# with recent activity. Designed to run as a `no_agent` Hermes cron — its stdout is
# delivered verbatim, zero LLM cost. Empty stdout is impossible by design; if there
# is nothing to report it prints a single "(No activity…)" line.
#
# Config (the only source of truth for repos/tokens/roster) is sourced from two files
# resolved relative to THIS script — environments.conf and roster.conf. See the
# matching .enxample templates. The engine itself is generic and edit-free.
#
# Usage:  ship_it_digest.sh [ENV_TYPE]      # env may also come from $ENV_TYPE
# Env:    SHIPIT_WINDOW_HOURS   look-back window in hours (default 24)
#         <token var>           per-env token, named by environments.conf
#         TARGET_GH_TOKEN       fallback token if the per-env var is empty
set -uo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Config: presets + roster -----------------------------------------------------
# Sourced, not parsed: config is bash, so adding an environment never touches the
# engine. .conf is the live copy; we fall back to the committed .enxample template so
# the script is runnable before anyone customizes it.
declare -gA SHIPIT_REPOS=() SHIPIT_TOKEN_VAR=() SHIPIT_EMOJI=()
for base in environments roster; do
  if [ -f "$script_dir/$base.conf" ]; then
    # shellcheck source=/dev/null
    source "$script_dir/$base.conf"
  elif [ -f "$script_dir/$base.enxample" ]; then
    # shellcheck source=/dev/null
    source "$script_dir/$base.enxample"
  fi
done

# --- Environment resolution -------------------------------------------------------
ENV_TYPE="${1:-${ENV_TYPE:-}}"
if [ -z "$ENV_TYPE" ]; then
  echo "ship-it-digest: no ENV_TYPE given (arg or env). Known: ${!SHIPIT_REPOS[*]:-none}" >&2
  exit 2
fi
if [ -z "${SHIPIT_REPOS[$ENV_TYPE]:-}" ]; then
  echo "ship-it-digest: unknown environment '$ENV_TYPE'. Known: ${!SHIPIT_REPOS[*]:-none}" >&2
  exit 2
fi

read -r -a TARGET_REPOS <<< "${SHIPIT_REPOS[$ENV_TYPE]}"
token_var="${SHIPIT_TOKEN_VAR[$ENV_TYPE]:-}"
export GH_TOKEN="${!token_var:-${TARGET_GH_TOKEN:-}}"

window_hours="${SHIPIT_WINDOW_HOURS:-24}"
SINCE="$(date -u -d "${window_hours} hours ago" +"%Y-%m-%dT%H:%M:%SZ")"

# --- Helpers ----------------------------------------------------------------------
# Per-run scratch for gh stderr, cleaned on exit (no shared /tmp path, no cross-repo race).
gh_err="$(mktemp -t shipit_err.XXXXXX)"
trap 'rm -f "$gh_err"' EXIT
run_gh() { gh "$@" 2>"$gh_err"; }

# Build sed args from the roster map once; word-boundary replace logins -> emoji.
# Unlisted names pass through unchanged. Empty roster => identity filter.
_emoji_sed_args=()
for _name in "${!SHIPIT_EMOJI[@]}"; do
  _emoji_sed_args+=(-e "s/\\b${_name}\\b/${SHIPIT_EMOJI[$_name]}/g")
done
emojify_names() {
  if [ "${#_emoji_sed_args[@]}" -eq 0 ]; then cat; else sed -E "${_emoji_sed_args[@]}"; fi
}

echo "🚢 Daily Ship-It Briefing — ${ENV_TYPE^^} (last ${window_hours}h)"

total_activity=0

for REPO in "${TARGET_REPOS[@]}"; do
  if ! run_gh repo view "$REPO" >/dev/null 2>&1; then
    echo -e "\n⚠️  Cannot access repository: $REPO"
    continue
  fi

  echo -e "\n--- Repository: $REPO ---"

  # --- Merged PRs ---
  merged=$(run_gh pr list -R "$REPO" --state merged --search "merged:>=$SINCE" \
    --json number,title,author,url,mergedBy \
    -q '.[] | "  • <\(.url)|#\(.number)> \(.title) (\(.author.name // .author.login | split(" ")[0])/\(.mergedBy.name // .mergedBy.login | split(" ")[0]))"')
  if [[ -n "$merged" ]]; then
    echo -e "\n✅ Merged PRs:"
    echo "$merged" | emojify_names
    total_activity=1
  fi

  # --- Currently Open PRs ---
  open_prs=$(run_gh pr list -R "$REPO" --state open \
    --json number,title,author,url,updatedAt,createdAt \
    -q '.[] | {number, title, url, author: (.author.name // .author.login | split(" ")[0]), created: .createdAt, updated: .updatedAt}')

  if [[ -n "$open_prs" && "$open_prs" != "[]" ]]; then
    echo -e "\n⏳ Currently Open PRs:"
    echo "$open_prs" | jq -r -c '. | if .created >= "'"$SINCE"'" then "  • <\(.url)|#\(.number)> NEW: \(.title) (\(.author))" else "  • <\(.url)|#\(.number)> \(.title) (\(.author))" end' | emojify_names

    # Per-PR activity. NOTE: fed via process substitution, NOT a pipe, so the loop
    # runs in THIS shell and total_activity assignments below actually stick. The
    # original `… | while` ran in a subshell and silently lost them.
    while read -r pr; do
      pr_num=$(echo "$pr" | jq -r '.number')
      pr_updated=$(echo "$pr" | jq -r '.updated')
      [[ "$pr_updated" < "$SINCE" ]] && continue

      pr_events=$(run_gh api "repos/$REPO/issues/$pr_num/events" --jq \
        '.[] | select(.created_at >= "'"$SINCE"'") |
        if .event == "labeled" then "      🏷️  Labeled " + .label.name + " by " + (.actor.login // "unknown")
        elif .event == "unlabeled" then "      🏷️  Unlabeled " + .label.name + " by " + (.actor.login // "unknown")
        elif .event == "assigned" then "      👤 Assigned to " + (.assignee.login // "unknown") + " by " + (.actor.login // "unknown")
        elif .event == "unassigned" then "      👤 Unassigned " + (.assignee.login // "unknown") + " by " + (.actor.login // "unknown")
        elif .event == "mentioned" then "      💬 Mentioned by " + (.actor.login // "unknown")
        elif .event == "referenced" then "      🔗 Referenced in commit by " + (.actor.login // "unknown")
        elif .event == "review_requested" then "      👀 Review requested from " + (.requested_reviewer.login // "unknown") + " by " + (.actor.login // "unknown")
        elif .event == "review_request_removed" then "      👀 Review request removed from " + (.requested_reviewer.login // "unknown") + " by " + (.actor.login // "unknown")
        elif .event == "ready_for_review" then "      ✅ Ready for review by " + (.actor.login // "unknown")
        elif .event == "convert_to_draft" then "      ✏️ Converted to draft by " + (.actor.login // "unknown")
        elif .event == "closed" then "      ✅ Closed by " + (.actor.login // "unknown")
        elif .event == "reopened" then "      🔄 Reopened by " + (.actor.login // "unknown")
        elif .event == "head_ref_deleted" then "      🔀 Branch deleted by " + (.actor.login // "unknown")
        elif .event == "head_ref_restored" then "      🔀 Branch restored by " + (.actor.login // "unknown")
        else "      🔄 " + .event + " by " + (.actor.login // "unknown")
        end' 2>/dev/null)

      pr_comments=$(run_gh api "repos/$REPO/issues/$pr_num/comments" --jq \
        '.[] | select(.created_at >= "'"$SINCE"'") | "      💬 Comment by " + (.user.login // "unknown")' 2>/dev/null)

      pr_reviews=$(run_gh api "repos/$REPO/pulls/$pr_num/reviews" --jq \
        '.[] | select(.submitted_at >= "'"$SINCE"'") |
        if .state == "APPROVED" then "      ✅ Approved by " + (.user.login // "unknown")
        elif .state == "CHANGES_REQUESTED" then "      ❌ Changes requested by " + (.user.login // "unknown")
        elif .state == "COMMENTED" then "      💬 Review comment by " + (.user.login // "unknown")
        else "      🔄 Review " + .state + " by " + (.user.login // "unknown")
        end' 2>/dev/null)

      all_activity=$(printf '%s\n%s\n%s\n' "$pr_events" "$pr_comments" "$pr_reviews")
      if [[ -n "${all_activity//[$'\n ']/}" ]]; then
        # Dedup (reviews can echo comments) and cap at 5 lines per PR to limit noise.
        echo "$all_activity" | sed '/^[[:space:]]*$/d' | sort -u | head -5 | emojify_names
      fi
    done < <(echo "$open_prs" | jq -c '.')

    total_activity=1
  fi

  # --- Issues with recent activity ---
  if run_gh repo view "$REPO" --json hasIssuesEnabled -q .hasIssuesEnabled | grep -q "true"; then
    issues_json=$(run_gh issue list -R "$REPO" --search "updated:>=$SINCE" \
      --json number,title,url,state,updatedAt,createdAt,author --limit 20)

    if [[ -n "$issues_json" && "$issues_json" != "[]" ]]; then
      echo -e "\n📝 Activity Updates:"

      while read -r issue; do
        issue_num=$(echo "$issue" | jq -r '.number')
        issue_title=$(echo "$issue" | jq -r '.title')
        issue_url=$(echo "$issue" | jq -r '.url')
        issue_author=$(echo "$issue" | jq -r '.author.login')
        issue_created=$(echo "$issue" | jq -r '.createdAt')

        if [[ "$issue_created" > "$SINCE" || "$issue_created" == "$SINCE" ]]; then
          echo "  • <${issue_url}|#${issue_num}> NEW: ${issue_title} (${issue_author})" | emojify_names
        else
          echo "  • <${issue_url}|#${issue_num}> ${issue_title} (${issue_author})" | emojify_names
        fi

        events=$(run_gh api "repos/$REPO/issues/$issue_num/events" --jq \
          '.[] | select(.created_at >= "'"$SINCE"'") |
          if .event == "labeled" then "    🏷️  Labeled " + .label.name + " by " + (.actor.login // "unknown")
          elif .event == "unlabeled" then "    🏷️  Unlabeled " + .label.name + " by " + (.actor.login // "unknown")
          elif .event == "assigned" then "    👤 Assigned to " + (.assignee.login // "unknown") + " by " + (.actor.login // "unknown")
          elif .event == "unassigned" then "    👤 Unassigned " + (.assignee.login // "unknown") + " by " + (.actor.login // "unknown")
          elif .event == "mentioned" then "    💬 Mentioned by " + (.actor.login // "unknown")
          elif .event == "referenced" then "    🔗 Referenced in commit by " + (.actor.login // "unknown")
          elif .event == "closed" then "    ✅ Closed by " + (.actor.login // "unknown")
          elif .event == "reopened" then "    🔄 Reopened by " + (.actor.login // "unknown")
          elif .event == "renamed" then "    ✏️ Renamed by " + (.actor.login // "unknown")
          elif .event == "milestoned" then "    🎯 Milestoned by " + (.actor.login // "unknown")
          elif .event == "demilestoned" then "    🎯 Demilestoned by " + (.actor.login // "unknown")
          else "    🔄 " + .event + " by " + (.actor.login // "unknown")
          end' 2>/dev/null)

        if [ -z "$events" ]; then
          if [[ "$issue_created" > "$SINCE" || "$issue_created" == "$SINCE" ]]; then
            echo "    🆕 Created by ${issue_author}" | emojify_names
          else
            comments=$(run_gh api "repos/$REPO/issues/$issue_num/comments" --jq \
              '.[] | select(.created_at >= "'"$SINCE"'") | "    💬 Comment by " + (.user.login // "unknown")' 2>/dev/null)
            if [[ -n "$comments" ]]; then
              echo "$comments" | emojify_names
            else
              echo "    🔄 Updated"
            fi
          fi
        else
          echo "$events" | emojify_names
        fi

        total_activity=1
      done < <(echo "$issues_json" | jq -c '.[]')
    fi
  fi
done

if [[ "$total_activity" -eq 0 ]]; then
  echo -e "\n(No activity in the last ${window_hours}h)"
fi
