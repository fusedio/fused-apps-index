"""The `reviews-sync` job (§18.2 step 9): review entries after merges.

Nothing fires on merge that this job can trust (a `pull_request: closed`
run of a hand merge is possible, but a merge made with GITHUB_TOKEN starts
no workflow), so this job runs on a schedule (every 10 minutes) and on
demand. It scans merged pull requests
since the newest review entry (minus a 2-day overlap), oldest first, and
then, for every row on main that still has no entry for its commit, the
merged PR behind that row's newest commit (so an entry that failed for
longer than the overlap is still written later). For each PR that changed
exactly one `apps/<slug>.json` (not a delete; a maintainer's request may
change other files besides) it reads the row as the merge left it, and when
`reviews/<slug>.json` has no entry for that row's commit it appends one
built from the PR's fused-review block — the BOT's comment only, for the
PR's head, status `pass` only (`needs-confirmation` = never confirmed):

    {"commit", "at", "whats_new", "new_powers": [str], "credits": [login],
     "pr": int, "first": bool, "claude"?: "skipped", "repo_id"?: int}
                                         (newest first)

`claude` copies the block's marker (only the fixed rules ran); `repo_id`
is GitHub's numeric id of the app repo, which the check compares on every
later release of the slug.

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


def entry_for(*, row: dict, pr: dict, block: dict, previous: list, credits: list,
              repo_id: int | None = None) -> dict:
    """`claude` is the block's marker ("skipped" = only the fixed rules
    ran); `repo_id` is GitHub's numeric id of the app repo, which the check
    compares on every later release of the slug (a repo re-made under the
    same name is not the same app)."""
    entry = {"commit": row["commit"], "at": pr.get("merged_at"),
             "whats_new": block.get("whats_new") or "",
             "new_powers": [p.get("detail") for p in block.get("new_powers") or []
                            if isinstance(p, dict) and p.get("detail")],
             "credits": credits, "pr": pr["number"], "first": not previous}
    if isinstance(block.get("claude"), str):
        entry["claude"] = block["claude"]
    if isinstance(repo_id, int):
        entry["repo_id"] = repo_id
    return entry


def accepts(block: dict | None, head_sha: str | None) -> bool:
    """A merged request's block becomes an entry only when it is the bot's
    review of the merged head and says `pass` — `needs-confirmation`
    means the author never confirmed (confirm.py rewrites it to pass)."""
    return bool(block) and block.get("sha") == head_sha and block.get("status") == "pass"


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


def sync_pr(gh: GitHub, repo: str, wt: str, number: int) -> bool:
    """Append the review entry for merged PR `number` when it changed
    exactly one row (not a delete; maintainer requests may carry other
    files too) and that row's commit has no entry yet. True = added."""
    try:
        pr = gh.pull(repo, number)
        files = gh.pull_files(repo, number)
    except GitHubError as e:
        print(f"#{number}: {e}", file=sys.stderr)
        return False
    if not pr.get("merged_at"):
        return False
    rows = [f for f in files if check.ROW_PATH.match(f.get("filename", ""))]
    if len(rows) != 1 or rows[0].get("status") == "removed":
        return False
    slug = check.ROW_PATH.match(rows[0]["filename"]).group(1)
    text = gh.file_text(repo, rows[0]["filename"], pr.get("merge_commit_sha") or "main")
    try:
        row = json.loads(text) if text else None
    except ValueError:
        row = None
    if not isinstance(row, dict) or not check.SHA_RE.match(str(row.get("commit", ""))):
        print(f"#{number}: no readable row for {slug}")
        return False
    previous = load_reviews(wt, slug, ROOT)
    if any(isinstance(e, dict) and e.get("commit") == row["commit"] for e in previous):
        return False
    bot = check.find_review_comment(gh, repo, number)
    block = check.parse_block((bot or {}).get("body") or "")
    if not accepts(block, (pr.get("head") or {}).get("sha")):
        print(f"#{number}: no passing (or confirmed) review block for its head; skipped")
        return False
    prev_commit = previous[0].get("commit") if previous else None
    meta = gh.repo(row["repo"]) if check.REPO_RE.match(str(row.get("repo", ""))) else None
    entry = entry_for(row=row, pr=pr, block=block, previous=previous,
                      credits=credits_for(gh, row["repo"], prev_commit, row["commit"]),
                      repo_id=(meta or {}).get("id"))
    os.makedirs(os.path.join(wt, "reviews"), exist_ok=True)
    path = os.path.join(wt, "reviews", f"{slug}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([entry] + previous, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    git("add", os.path.join("reviews", f"{slug}.json"), cwd=wt)
    git("commit", "-q", "-m", f"review: {slug} @ {row['commit'][:7]}", cwd=wt)
    print(f"#{number}: review entry for {slug} @ {row['commit'][:7]}")
    return True


def lacking(wt: str) -> list:
    """Slugs whose row on main has no review entry for its commit."""
    out = []
    adir = os.path.join(ROOT, "apps")
    for name in sorted(os.listdir(adir)) if os.path.isdir(adir) else []:
        m = check.ROW_PATH.match(f"apps/{name}")
        if not m:
            continue
        try:
            with open(os.path.join(adir, name), encoding="utf-8") as fh:
                row = json.load(fh)
        except (OSError, ValueError):
            continue
        commit = row.get("commit") if isinstance(row, dict) else None
        if isinstance(commit, str) and not any(
                isinstance(e, dict) and e.get("commit") == commit
                for e in load_reviews(wt, m.group(1), ROOT)):
            out.append(m.group(1))
    return out


def merged_pr_for_row(gh: GitHub, repo: str, slug: str) -> int | None:
    """The merged PR behind main's newest commit to `apps/<slug>.json`."""
    try:
        commits = gh.get(f"/repos/{repo}/commits",
                         params={"path": f"apps/{slug}.json", "sha": "main", "per_page": 1})
        if not commits:
            return None
        pulls = gh.commit_pulls(repo, commits[0]["sha"])
    except (GitHubError, KeyError, TypeError) as e:
        print(f"{slug}: could not find its pull request: {e}", file=sys.stderr)
        return None
    merged = [p for p in pulls if p.get("merged_at")]
    return merged[0]["number"] if merged else None


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
    seen = set()
    for item in prs:
        seen.add(item["number"])
        added += sync_pr(gh, repo, wt, item["number"])
    # The window above starts at the newest entry: a release whose entry
    # failed for longer than the overlap would never be looked at again.
    # Every row still lacking an entry gets its own PR looked up directly.
    for slug in lacking(wt):
        number = merged_pr_for_row(gh, repo, slug)
        if number and number not in seen:
            seen.add(number)
            added += sync_pr(gh, repo, wt, number)
    if added or not existed:
        if existed:
            git("pull", "--rebase", "-q", "origin", BRANCH, cwd=wt)
        git("push", "-q", "origin", f"HEAD:refs/heads/{BRANCH}", cwd=wt)
    print(f"{added} review entr{'y' if added == 1 else 'ies'} added"
          + ("" if existed else f"; started the {BRANCH} branch"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
