#!/usr/bin/env bash
# One-time setup of the index repo, run by the owner (needs admin on it):
#
#   scripts/setup_ruleset.sh [owner/name]     (default vasu2001/fused-apps-index)
#
# 1. Repo settings: squash merges only, no auto-merge (a maintainer merges
#    every pull request by hand; owner, 2026-10-26), branches deleted after
#    merge.
# 2. The review labels.
# 3. The `main` ruleset: pull request required, required status check
#    `review` (the commit status check.py sets on the PR head, from GitHub
#    Actions), squash only, no force pushes or deletion. NO bypass actors,
#    admins included. GitHub refuses the GitHub Actions app as a bypass actor
#    on a user-owned repo ("Actor GitHub Actions integration must be part of
#    the ruleset source or owner organization"), so the reviews-sync job does
#    not write main: it pushes review entries to the `reviews` branch, which
#    this ruleset does not cover (~DEFAULT_BRANCH only).
# 4. Reminder: the ANTHROPIC_API_KEY secret (optional for now; without it
#    Claude's review is skipped and only the fixed rules run).
set -euo pipefail

REPO="${1:-vasu2001/fused-apps-index}"

echo "== repo settings ($REPO)"
gh api -X PATCH "repos/$REPO" \
  -F allow_auto_merge=false \
  -F allow_squash_merge=true \
  -F allow_merge_commit=false \
  -F allow_rebase_merge=false \
  -F delete_branch_on_merge=true >/dev/null

echo "== labels"
gh label create review-passed      --repo "$REPO" --color 2da44e --description "The review check passed" --force
gh label create needs-confirmation --repo "$REPO" --color d29922 --description "New powers: waiting for the author's fused: confirm" --force
gh label create review-failed      --repo "$REPO" --color cf222e --description "The review check failed" --force

APP_ID="$(gh api /apps/github-actions --jq .id)"   # 15368 on github.com
echo "== GitHub Actions app id: $APP_ID"

EXISTING="$(gh api "repos/$REPO/rulesets" --jq '.[] | select(.name=="main") | .id' || true)"
BODY="$(cat <<JSON
{
  "name": "main",
  "target": "branch",
  "enforcement": "active",
  "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
  "bypass_actors": [],
  "rules": [
    {"type": "deletion"},
    {"type": "non_fast_forward"},
    {"type": "pull_request", "parameters": {
      "required_approving_review_count": 0,
      "dismiss_stale_reviews_on_push": false,
      "require_code_owner_review": false,
      "require_last_push_approval": false,
      "required_review_thread_resolution": false,
      "allowed_merge_methods": ["squash"]}},
    {"type": "required_status_checks", "parameters": {
      "strict_required_status_checks_policy": false,
      "required_status_checks": [{"context": "review", "integration_id": $APP_ID}]}}
  ]
}
JSON
)"
if [ -n "$EXISTING" ]; then
  echo "== updating ruleset $EXISTING"
  echo "$BODY" | gh api -X PUT "repos/$REPO/rulesets/$EXISTING" --input - >/dev/null
else
  echo "== creating ruleset"
  echo "$BODY" | gh api -X POST "repos/$REPO/rulesets" --input - >/dev/null
fi

cat <<EOF

Done. When you want Claude's review on, set the key (this script can't):

  gh secret set ANTHROPIC_API_KEY --repo $REPO

Until then the check skips Claude's review (it says so on each request) and
runs every other step; clean listings still merge.
EOF
