"""The `confirm` job (§18.2 step 7): an author's `fused: confirm <sha>`.

A release whose review found new powers passes the check but waits. The
PR author confirms by commenting exactly `fused: confirm <head sha>` (fused
posts it for them). This job accepts the confirmation only when ALL hold:

- the comment is on an open pull request, by that pull request's author;
- `<sha>` is the pull request's current head;
- the bot's own fused-review comment (never one a person wrote) is for
  that head and says `needs-confirmation`.

Then it turns the block's status to `pass` (fused shows Live once merged)
and labels the PR `review-passed`. It never merges: a maintainer merges by
hand (owner, 2026-10-26). A later push re-runs the check, whose fresh block
says `needs-confirmation` again, so a confirmation cannot be ridden.
Anything else: no action, with the reason in the job log.
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check  # noqa: E402
from github import GitHub  # noqa: E402

CONFIRM_RE = re.compile(r"^\s*fused:\s*confirm\s+([0-9a-f]{40})\s*$")


def decide(event: dict, pr: dict, bot_comment: dict | None) -> tuple:
    """`(ok, reason_or_sha)` — pure, so the rule is testable without GitHub."""
    comment = event.get("comment") or {}
    issue = event.get("issue") or {}
    m = CONFIRM_RE.match(comment.get("body") or "")
    if "pull_request" not in issue or not m:
        return False, "not a confirm comment on a pull request"
    sha = m.group(1)
    who = (comment.get("user") or {}).get("login") or ""
    if who.lower() != ((pr.get("user") or {}).get("login") or "").lower():
        return False, f"{who} is not the author of this pull request"
    if pr.get("state") != "open":
        return False, "the pull request is not open"
    if (pr.get("head") or {}).get("sha") != sha:
        return False, "that is not the pull request's current version"
    block = check.parse_block((bot_comment or {}).get("body") or "")
    if not block or block.get("sha") != sha:
        return False, "there is no review of that version yet"
    if block.get("status") != "needs-confirmation":
        return False, f"the review says {block.get('status')!r}, nothing to confirm"
    return True, sha


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as fh:
        event = json.load(fh)
    number = (event.get("issue") or {}).get("number")
    if not number or "pull_request" not in (event.get("issue") or {}):
        print("not a pull request comment")
        return 0
    gh = GitHub()
    pr = gh.pull(repo, number)
    bot = check.find_review_comment(gh, repo, number)
    ok, why = decide(event, pr, bot)
    if not ok:
        print(f"no action: {why}")
        return 0
    sha = why
    block = check.parse_block(bot["body"])
    result = {"sha": sha, "status": "pass", "reasons": [],
              "whats_new": block.get("whats_new") or "",
              "new_powers": block.get("new_powers") or [], "kind": "confirmed"}
    if block.get("claude"):
        result["claude"] = block["claude"]
    gh.edit_comment(repo, bot["id"], check.render_comment(
        result, confirmed_by=pr["user"]["login"]))
    check.set_label(gh, repo, number, "review-passed")
    try:
        gh.react(repo, event["comment"]["id"], "+1")
    except Exception as e:  # noqa: BLE001 — a reaction is decoration
        print(f"reaction failed: {e}", file=sys.stderr)
    print(f"confirmed {sha}; a maintainer merges it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
