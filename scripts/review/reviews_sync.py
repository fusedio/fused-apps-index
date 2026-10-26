"""The `reviews-sync` job (§18.2 step 9): review entries after merges.

Merges done by auto-merge are made with GITHUB_TOKEN, and events caused by
GITHUB_TOKEN start no workflow, so nothing fires on merge. This job runs on
a schedule (every 10 minutes) and on demand. It scans merged pull requests
since the newest review entry (minus a 2-day overlap), oldest first; for
each one that changed exactly one `apps/<slug>.json` (not a delete) it
reads the row as the merge left it, and when `reviews/<slug>.json` has no
entry for that row's commit it appends one built from the PR's fused-review
block — the BOT's comment only, for the PR's head:

    {"commit", "at", "whats_new", "new_powers": [str], "credits": [login],
     "pr": int, "first": bool}          (newest first)

`credits` = authors of pull requests merged into the app's repo between the
previous approved commit and this one (compare → commits → PRs by sha; best
effort, empty when unknown).

WHERE ENTRIES GO: the `reviews` BRANCH, not main. main's ruleset has no
bypass actor (GitHub refuses the GitHub Actions app as a bypass actor on a
user-owned repo), so the bot cannot commit to main. The `reviews` branch
holds only `reviews/` (+ a README); when it does not exist yet it is
started as a fresh history seeded from main's `reviews/`. Each entry is one
bot commit "review: <slug> @ <sha7>" there, pushed plainly (no PR). main's
`reviews/` (hand entries) stays as the fallback: a slug with no file on the
branch starts from main's file. The backend reads the branch first.

A PR without a usable block is skipped (logged); the catalog serves that
row with `whats_new: null` until someone fixes it.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import check  # noqa: E402
from github import GitHub, GitHubError  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BRANCH = "reviews"
OVERLAP = dt.timedelta(days=2)
MAX_CREDIT_COMMITS = 40
BRANCH_README = """# reviews

Written by the index's `reviews-sync` job only: one `reviews/<slug>.json`
per listed app, each release's What's new, newest first. Nothing else lives
on this branch. `main`'s `reviews/` is the fallback for slugs not here yet.
"""


def _read(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, ValueError):
        return None
    return data if isinstance(data, list) else None


def load_reviews(branch_dir: str, slug: str, fallback_dir: str | None = None) -> list:
    """The slug's entries on the branch, else main's (fallback), else []."""
    data = _read(os.path.join(branch_dir, "reviews", f"{slug}.json"))
    if data is None and fallback_dir:
        data = _read(os.path.join(fallback_dir, "reviews", f"{slug}.json"))
    return data or []


def since(*dirs: str) -> str:
    """ISO date to search from: the newest entry's `at` minus the overlap."""
    newest = None
    for root in dirs:
        rdir = os.path.join(root, "reviews")
        for name in os.listdir(rdir) if os.path.isdir(rdir) else []:
            if not name.endswith(".json"):
                continue
            for e in load_reviews(root, name[:-5]):
                at = e.get("at") if isinstance(e, dict) else None
                if isinstance(at, str) and (newest is None or at > newest):
                    newest = at
    if newest is None:
        return "2026-01-01"
    try:
        t = dt.datetime.fromisoformat(newest.replace("Z", "+00:00"))
    except ValueError:
        return "2026-01-01"
    return (t - OVERLAP).date().isoformat()


def entry_for(*, row: dict, pr: dict, block: dict, previous: list, credits: list) -> dict:
    return {"commit": row["commit"], "at": pr.get("merged_at"),
            "whats_new": block.get("whats_new") or "",
            "new_powers": [p.get("detail") for p in block.get("new_powers") or []
                           if isinstance(p, dict) and p.get("detail")],
            "credits": credits, "pr": pr["number"], "first": not previous}


def credits_for(gh: GitHub, repo: str, prev: str | None, commit: str) -> list:
    """Logins of PR authors merged into `repo` between `prev` and `commit`."""
    if not prev or prev == commit:
        return []
    owner = repo.split("/", 1)[0].lower()
    try:
        data = gh.compare(repo, prev, commit) or {}
    except GitHubError:
        return []
    logins: set = set()
    for c in (data.get("commits") or [])[-MAX_CREDIT_COMMITS:]:
        try:
            pulls = gh.commit_pulls(repo, c["sha"])
        except (GitHubError, KeyError):
            continue
        for p in pulls:
            login = ((p.get("user") or {}).get("login") or "")
            if p.get("merged_at") and login and login.lower() != owner \
                    and not login.endswith("[bot]"):
                logins.add(login)
    return sorted(logins)


def git(*args: str, cwd: str = ROOT, check_ok: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", cwd, *args], check=check_ok, capture_output=True,
                          text=True)


def open_branch(path: str) -> bool:
    """A worktree of the `reviews` branch at `path`. Returns whether the
    branch already existed on origin (False = started here, seeded from
    main's reviews/)."""
    if os.path.exists(path):
        git("worktree", "remove", "--force", path, check_ok=False)
        shutil.rmtree(path, ignore_errors=True)
    exists = git("ls-remote", "--exit-code", "--heads", "origin", BRANCH,
                 check_ok=False).returncode == 0
    if exists:
        git("fetch", "-q", "origin", f"+refs/heads/{BRANCH}:refs/remotes/origin/{BRANCH}")
        git("worktree", "add", "-q", "--force", "-B", BRANCH, path, f"origin/{BRANCH}")
        return True
    git("worktree", "add", "-q", "--detach", path, "HEAD")
    git("checkout", "-q", "--orphan", BRANCH, cwd=path)
    git("rm", "-rq", "--cached", ".", cwd=path, check_ok=False)
    for name in os.listdir(path):
        if name in (".git", "reviews"):
            continue
        full = os.path.join(path, name)
        shutil.rmtree(full) if os.path.isdir(full) else os.remove(full)
    with open(os.path.join(path, "README.md"), "w", encoding="utf-8") as fh:
        fh.write(BRANCH_README)
    git("add", "-A", ".", cwd=path)
    main_sha = git("rev-parse", "--short", "HEAD").stdout.strip()
    git("commit", "-q", "--allow-empty", "-m",
        f"reviews: start from main's reviews/ ({main_sha})", cwd=path)
    return False


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    gh = GitHub()
    wt = os.environ.get("REVIEWS_DIR") or os.path.join(
        os.environ.get("RUNNER_TEMP") or tempfile.gettempdir(), "fused-reviews-branch")
    existed = open_branch(wt)
    query = f"repo:{repo} is:pr is:merged base:main merged:>={since(wt, ROOT)}"
    # Oldest merge first, so entries stack newest-first in merge order.
    prs = sorted(gh.search_issues(query), key=lambda i: i.get("closed_at") or "")
    added = 0
    for item in prs:
        number = item["number"]
        try:
            pr = gh.pull(repo, number)
            files = gh.pull_files(repo, number)
        except GitHubError as e:
            print(f"#{number}: {e}", file=sys.stderr)
            continue
        rows = [f for f in files if check.ROW_PATH.match(f.get("filename", ""))]
        if len(rows) != 1 or len(files) != 1 or rows[0].get("status") == "removed":
            continue
        slug = check.ROW_PATH.match(rows[0]["filename"]).group(1)
        text = gh.file_text(repo, rows[0]["filename"], pr.get("merge_commit_sha") or "main")
        try:
            row = json.loads(text) if text else None
        except ValueError:
            row = None
        if not isinstance(row, dict) or not check.SHA_RE.match(str(row.get("commit", ""))):
            print(f"#{number}: no readable row for {slug}")
            continue
        previous = load_reviews(wt, slug, ROOT)
        if any(isinstance(e, dict) and e.get("commit") == row["commit"] for e in previous):
            continue
        bot = check.find_review_comment(gh, repo, number)
        block = check.parse_block((bot or {}).get("body") or "")
        if not block or block.get("sha") != (pr.get("head") or {}).get("sha") or \
                block.get("status") not in ("pass", "needs-confirmation"):
            print(f"#{number}: no passing review block for its head; skipped")
            continue
        prev_commit = previous[0].get("commit") if previous else None
        entry = entry_for(row=row, pr=pr, block=block, previous=previous,
                          credits=credits_for(gh, row["repo"], prev_commit, row["commit"]))
        os.makedirs(os.path.join(wt, "reviews"), exist_ok=True)
        path = os.path.join(wt, "reviews", f"{slug}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump([entry] + previous, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        git("add", os.path.join("reviews", f"{slug}.json"), cwd=wt)
        git("commit", "-q", "-m", f"review: {slug} @ {row['commit'][:7]}", cwd=wt)
        added += 1
        print(f"#{number}: review entry for {slug} @ {row['commit'][:7]}")
    if added or not existed:
        if existed:
            git("pull", "--rebase", "-q", "origin", BRANCH, cwd=wt)
        git("push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}", cwd=wt)
    print(f"{added} review entr{'y' if added == 1 else 'ies'} added"
          + ("" if existed else f"; started the {BRANCH} branch"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
