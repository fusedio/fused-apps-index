"""The review check (§18.2 steps 1-8): the `review` job of review.yml.

Runs from the BASE branch's checkout on `pull_request_target`. The pull
request and the app it points at are DATA: nothing from either is ever
imported, executed or checked out. Each step is a hard fail with a
one-line plain reason; an error anywhere is a fail too (M3).

    1 Files   only `apps/<slug>.json`, one slug (maintainers: see below)
    2 Who     the PR author owns `row.repo` (old and new row), or is a
              PUBLIC member of the owning org — by the repo's identity:
              GitHub's answer for the repo must carry the row's own name
              (a renamed or moved repo fails) and its owner login is the
              owner; a repo whose numeric id differs from the one the
              slug's review entries recorded fails too
    3 Shape   the row schema, unique slug and repo, remix_of exists, branch
              = the repo's default branch, icon/preview exist at commit,
              slug = the `fused-app-id` the entry page declares at commit
              (`app_id.py`; checked once the app's files are read)
    4 Which commit  compare `<branch>...<commit>` is identical or behind
    5 Fixed rules   `rules.hard_hits` over the app at commit (+ the listing
              text): any hit fails, nothing overrules it
    6 Claude's review   whole app (first listing) or the diff from the
              previous approved commit (update). SKIPPED (not failed) while
              ANTHROPIC_API_KEY is unset or empty (owner's call): the block
              then carries `"claude": "skipped"` (reviews_sync copies it into
              the review entry, the backend serves it), What's new comes from
              the request's own text, and new powers from `rules.powers`
              alone. With a key, an API error, timeout or invalid answer
              still fails (M3).
    7 New powers  `rules.powers` ∪ the model's list: non-empty passes the
              review but the `review` status stays PENDING (unmergeable)
              until the author's `fused: confirm <sha>` (confirm.py sets it
              to success). GATE OFF for now (`REQUIRE_CONFIRM = False`):
              the powers are listed, the result is `pass`, nothing waits.
    8 Result  ONE comment with the `<!-- fused-review {...} -->` block,
              labels, the `review` commit status on the PR head (pending
              while running; success on pass; pending on
              needs-confirmation; failure). NOT ENFORCED for now
              (`ENFORCE = False`): the status is always success, so a
              failed review is recorded but does not block the merge.
              NOTHING MERGES ON ITS OWN: a maintainer merges every pull
              request by hand once the check is green (owner, 2026-10-26;
              the earlier `gh pr merge --auto` on a clean pass is gone).

Pull requests that change anything but one row (block list, collections,
categories, the check itself) are maintainer changes: from anyone without
write access to the index they fail step 1. From a maintainer: a changed
`blocklist.json` must match its schema (step 3); an `apps/<slug>.json` the
request also changes runs steps 2-7 like an author's (one row per request;
a maintainer may delist any row); the rest passes with "a person reviews
and merges this".

Usage (the workflow): `python scripts/review/check.py` with GITHUB_EVENT_PATH,
GITHUB_REPOSITORY, GITHUB_TOKEN, GH_TOKEN and ANTHROPIC_API_KEY set.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app_id  # noqa: E402
import claude_review  # noqa: E402
import fetch_app  # noqa: E402
import rules  # noqa: E402
from github import BOT_LOGIN, GitHub, GitHubError  # noqa: E402

STEP_NAMES = {1: "Files", 2: "Who", 3: "Shape", 4: "Which commit", 5: "Fixed rules",
              6: "Claude's review", 7: "New powers"}
ROW_KEYS = ("slug", "repo", "branch", "commit", "name", "description", "category",
            "tags", "icon", "preview", "remix_of", "version")
ROW_PATH = re.compile(r"^apps/([a-z0-9][a-z0-9-]{1,63})\.json$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
VERSION_RE = re.compile(r"^\d{1,6}(?:\.\d{1,6}){0,3}(?:[-+][0-9A-Za-z.-]{1,32})?$")
ASSET_RE = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[^\\\0\n]{1,200}"
                      r"\.(?:png|jpe?g|gif|webp|svg|avif)$", re.I)
BLOCK_RE = re.compile(r"<!-- fused-review (\{.*?\}) -->", re.S)
LABELS = ("review-passed", "needs-confirmation", "review-failed")
#: The confirm gate (step 7). OFF for now (owner, 2026-10-27: "relax it"):
#: new powers are still found and listed in the comment and the block, but
#: the result is `pass` and the `review` status goes green without the
#: author's `fused: confirm`. Flip to True to bring the gate (and the
#: `confirm` job in review.yml) back.
REQUIRE_CONFIRM = False
#: Enforcement. OFF for now (owner, 2026-10-27): the check still runs every
#: step, posts its comment, sets its label and exits 0, but the required
#: `review` status on the head is ALWAYS success (its text says what the
#: review found), so a failed review does not block the merge. The index
#: is taking in many apps and the false positives get looked at later.
#: Flip to True to make a fail block the merge again.
ENFORCE = False
# Anyone with write access to the index is a maintainer (owner, 2026-10-27:
# "simplify it" — the index is an org repo now, and write access there IS
# membership). Everyone else is an author: one row per request, steps 2-7.
MAINTAINER_ROLES = {"admin", "maintain", "write"}
CLAUDE_OFF = "Claude's review is off (no key set); only the fixed rules ran."
CRASH_REASON = ("The review ran into a problem and could not finish. It runs again "
                "when the listing is updated.")


class Fail(Exception):
    def __init__(self, step: int, reasons, whats_new: str = ""):
        super().__init__(step)
        self.step = step
        self.reasons = [reasons] if isinstance(reasons, str) else list(reasons)
        self.whats_new = whats_new


# ------------------------------------------------------------------ inputs


class Index:
    """The index as the BASE branch has it (the checkout on disk)."""

    def __init__(self, root: str, extra_rows: dict | None = None):
        self.root = root
        with open(os.path.join(root, "categories.json"), encoding="utf-8") as fh:
            self.categories = {c["id"] for c in json.load(fh)}
        self.rows: dict = {}
        apps = os.path.join(root, "apps")
        for name in sorted(os.listdir(apps)) if os.path.isdir(apps) else []:
            m = ROW_PATH.match(f"apps/{name}")
            if not m:
                continue
            try:
                with open(os.path.join(apps, name), encoding="utf-8") as fh:
                    row = json.load(fh)
            except (OSError, ValueError):
                continue
            if isinstance(row, dict):
                self.rows[m.group(1)] = row
        self.rows.update(extra_rows or {})


class LiveSource:
    """Everything the steps ask the outside world, answered by GitHub,
    codeload and the Anthropic API. The fixtures swap in a fake."""

    def __init__(self, gh: GitHub, index_repo: str, event: dict):
        p = event["pull_request"]
        self.gh = gh
        self.index_repo = index_repo
        self.pr = {"number": p["number"], "author": p["user"]["login"],
                   "head_sha": p["head"]["sha"],
                   "head_repo": (p["head"].get("repo") or {}).get("full_name") or index_repo,
                   "title": p.get("title") or "", "body": p.get("body") or ""}

    def pr_files(self) -> list:
        return self.gh.pull_files(self.index_repo, self.pr["number"])

    def head_text(self, path: str) -> str | None:
        return self.gh.file_text(self.pr["head_repo"], path, self.pr["head_sha"])

    def user_type(self, login: str):
        return self.gh.user_type(login)

    def is_public_member(self, org: str, login: str) -> bool:
        return self.gh.is_public_member(org, login)

    def is_maintainer(self, login: str) -> bool:
        return self.gh.permission(self.index_repo, login) in MAINTAINER_ROLES

    def repo_meta(self, repo: str):
        return self.gh.repo(repo)

    def compare_status(self, repo: str, base: str, head: str):
        data = self.gh.compare(repo, base, head)
        return data.get("status") if data else None

    def app_tree(self, repo: str, sha: str) -> list:
        return fetch_app.fetch_tree(repo, sha, token=self.gh.token or None)

    def review_entries(self, slug: str) -> list:
        """The slug's review entries on the bot's `reviews` branch ([] when
        it has none). A read error propagates: `main` fails closed."""
        text = self.gh.file_text(self.index_repo, f"reviews/{slug}.json", "reviews")
        try:
            data = json.loads(text) if text else []
        except ValueError:
            return []
        return data if isinstance(data, list) else []

    def review(self, **kw):
        return claude_review.review(**kw)


# ------------------------------------------------------------------- steps


def run_steps(src, index: Index) -> dict:
    """The check's verdict: `{"sha", "status", "reasons", "whats_new",
    "new_powers", "step", "slug", "kind"}`. `step` names the failing step
    (None on a pass). Never raises for a PR's content; GitHub or network
    errors propagate to `main`, which fails closed."""
    pr = src.pr
    out = {"sha": pr["head_sha"], "status": "pass", "reasons": [], "whats_new": "",
           "new_powers": [], "step": None, "slug": None, "kind": None}
    try:
        out.update(_steps(src, index, pr))
    except Fail as f:
        out.update(status="fail", step=f.step, reasons=f.reasons[:8],
                   whats_new=f.whats_new, new_powers=[])
    return out


def _steps(src, index: Index, pr: dict) -> dict:
    author = pr["author"]
    # 1. Files
    rows, others = [], []
    for f in src.pr_files():
        name, status = f.get("filename", ""), f.get("status", "")
        m = ROW_PATH.match(name)
        if m and status in ("added", "modified", "removed"):
            rows.append((m.group(1), status))
        else:
            others.append(name)
            if f.get("previous_filename"):
                others.append(f["previous_filename"])
    maintainer = False
    if others or not rows:
        if not src.is_maintainer(author):
            if not rows:
                raise Fail(1, "This request doesn't change an app listing (apps/<name>.json).")
            shown = ", ".join(sorted(set(others))[:5])
            raise Fail(1, f"This request changes files other than one app listing ({shown}). "
                          "Only Fused maintainers change those.")
        # A person reviews a maintainer change, but the block list is read
        # by every copy of every app: a typo there must fail here, not
        # quietly reach the backend (which then serves its last good list).
        if "blocklist.json" in others:
            problems = blocklist_problems(src.head_text("blocklist.json"))
            if problems:
                raise Fail(3, problems)
        if not rows:
            return {"kind": "maintainer",
                    "whats_new": "A maintainer change. A person reviews and merges it."}
        # A listing change inside a maintainer request gets the full review
        # below, like any author's (its commit would otherwise merge
        # unreviewed and still show as reviewed).
        maintainer = True
    if len(rows) > 1:
        raise Fail(1, "One app per request: this request changes the listings of "
                      + ", ".join(sorted(s for s, _ in rows)[:5]) + ".")
    slug, status = rows[0]
    old = index.rows.get(slug)

    if status == "removed":
        if old is None:
            raise Fail(1, f"There is no listing called {slug} to remove.")
        old_repo = str(old.get("repo") or "")
        if maintainer:
            # Taking a listing down merges no code; maintainers may do it
            # for any app (moderation), a person still merges it.
            return {"slug": slug, "kind": "delist",
                    "whats_new": "Removed from the Marketplace by a Fused maintainer."}
        old_meta = src.repo_meta(old_repo) if REPO_RE.match(old_repo) else None
        # A repo that is gone (deleted) can still be delisted by the owner
        # its row names; one that answers must still be that same repo.
        _who(src, author, old_repo, old_meta if isinstance(old_meta, dict) else None)
        return {"slug": slug, "kind": "delist",
                "whats_new": "Removed from the Marketplace by its author."}

    text = src.head_text(f"apps/{slug}.json")
    try:
        row = json.loads(text) if text is not None else None
    except ValueError:
        row = None
    if not isinstance(row, dict):
        raise Fail(3, "The listing file isn't a valid JSON object.")
    repo = row.get("repo")
    if not isinstance(repo, str) or not REPO_RE.match(repo):
        raise Fail(3, "The listing's repo must be written as owner/name.")
    meta = src.repo_meta(repo)
    if not isinstance(meta, dict) or meta.get("private"):
        raise Fail(3, f"{repo} can't be found, or it isn't public.")

    # 2. Who — by the repo's identity, not only the text of its name.
    _who(src, author, repo, meta)
    if old is not None and status == "modified":
        old_repo = str(old.get("repo") or "")
        if old_repo.lower() != repo.lower():
            old_meta = src.repo_meta(old_repo) if REPO_RE.match(old_repo) else None
            _who(src, author, old_repo, old_meta if isinstance(old_meta, dict) else None)
    _same_repo_as_before(src, slug, repo, meta)

    # 3. Shape
    problems = shape_problems(row, slug, index, status)
    if problems:
        raise Fail(3, problems)
    if row["branch"] != meta.get("default_branch"):
        raise Fail(3, f"The listing must follow {repo}'s main branch "
                      f"({meta.get('default_branch')}), not {row['branch']}.")

    # 4. Which commit
    state = src.compare_status(repo, row["branch"], row["commit"])
    if state is None:
        raise Fail(4, f"That version of the app ({row['commit'][:7]}) can't be found in {repo}.")
    if state not in ("identical", "behind"):
        raise Fail(4, f"That version of the app ({row['commit'][:7]}) isn't on {repo}'s "
                      f"{row['branch']} branch. A version that exists only in someone "
                      "else's copy can't be listed.")

    try:
        tree = src.app_tree(repo, row["commit"])
    except fetch_app.FetchError as e:
        raise Fail(5, str(e)) from None
    _slug_is_app_id(tree, slug)
    paths = {e["path"] for e in tree if e.get("mode") != "120000"}
    for key in ("icon", "preview"):
        if row[key] is not None and row[key] not in paths:
            raise Fail(3, f"The {key} {row[key]} isn't in the app at that version.")

    # 5. Fixed rules
    hits = rules.hard_hits(tree)
    listing_text = rules.static_hits(diff="", files=[], title=str(row["name"]),
                                     body=str(row["description"]) + "\n" + " ".join(row["tags"]))
    if listing_text:
        hits.append({"rule": "ai-text", "detail": "The listing's name or description "
                     "contains text addressed to an AI", "file": None, "line": None})
    if hits:
        raise Fail(5, [hit_reason(h) for h in hits])

    # 6. Claude's review
    same_app = old is not None and str(old.get("repo", "")).lower() == repo.lower()
    if same_app and old.get("commit") == row["commit"]:
        return {"slug": slug, "kind": "details", "whats_new": "Listing details updated."}
    diff = None
    if same_app and isinstance(old.get("commit"), str) and SHA_RE.match(old["commit"]):
        try:
            prev = src.app_tree(repo, old["commit"])
        except fetch_app.FetchError as e:
            raise Fail(6, f"The last approved version could not be read: {e}") from None
        diff = fetch_app.diff_trees(prev, tree)
    claude = None
    if not os.environ.get("ANTHROPIC_API_KEY"):
        # The owner runs without a key for now: every other step still
        # holds; this one is skipped and says so on the request.
        claude = "skipped"
        answer = {"verdict": "pass", "reasons": [], "new_powers": [],
                  "whats_new": fallback_whats_new(pr)}
    else:
        answer, why = src.review(row=row, pr=pr, tree=None if diff is not None else tree,
                                 diff=diff)
        if answer is None:
            raise Fail(6, why)
        if answer["verdict"] != "pass":
            raise Fail(6, answer["reasons"], answer["whats_new"])

    # 7. New powers
    found = rules.powers(diff if diff is not None else tree) + answer["new_powers"]
    powers, seen = [], set()
    for p in found:
        key = p["detail"].strip().lower()
        if key not in seen:
            seen.add(key)
            powers.append({"kind": p["kind"], "detail": p["detail"]})
    out = {"slug": slug, "kind": "update" if diff is not None else "list",
           "whats_new": answer["whats_new"], "new_powers": powers[:16],
           "status": "needs-confirmation" if powers and REQUIRE_CONFIRM else "pass"}
    if claude:
        out["claude"] = claude
    return out


_WHAT_CHANGED = re.compile(r"^\s*(?:#+\s*|\*\*)?what\s+changed\b\W*", re.I)


def fallback_whats_new(pr: dict) -> str:
    """What's new without Claude: the request body's "What changed" text
    when it has that heading, else the body's first line, else the title."""
    lines = [ln.strip() for ln in str(pr.get("body") or "").splitlines()]
    text = ""
    for i, ln in enumerate(lines):
        m = _WHAT_CHANGED.match(ln)
        if m:
            rest = ln[m.end():].strip(" *:")
            para = [rest] if rest else []
            for nxt in lines[i + 1:]:
                if not nxt:
                    if para:
                        break
                    continue
                if nxt.startswith("#"):
                    break
                para.append(nxt)
            text = " ".join(para)
            break
    if not text:
        text = next((ln for ln in lines if ln and not ln.startswith("<!--")), "")
    if not text:
        text = str(pr.get("title") or "")
    text = re.sub(r"^\s*(?:[#>]+|[-*+]|\d+[.)])\s+", "", text)
    text = re.sub(r"\*\*|__|`", "", " ".join(text.split()))
    return text[:700]


def _who(src, author: str, repo: str, meta: dict | None = None) -> None:
    """The PR author may publish `repo`. With `meta` (GitHub's answer for
    the repo) the check is by identity: GitHub follows a renamed or moved
    repo's old name silently, so the name GitHub answers must be the one
    the row names, and the owner is the account GitHub reports, not the
    text before the slash."""
    owner = repo.split("/", 1)[0]
    if meta is not None:
        full = str(meta.get("full_name") or "")
        if full.lower() != repo.lower():
            raise Fail(2, f"{repo} now lives at {full or 'another address'} on GitHub (it was "
                          "renamed or moved). The listing must name the repo where it lives; "
                          "a Fused maintainer can move an existing listing.")
        owner = str((meta.get("owner") or {}).get("login") or "")
    if owner and owner.lower() == author.lower():
        return
    if owner and src.user_type(owner) == "Organization":
        if src.is_public_member(owner, author):
            return
        raise Fail(2, f"{repo} belongs to the {owner} organization, which doesn't show "
                      f"{author} as a public member. Make your membership public and "
                      "try again.")
    raise Fail(2, f"Only {owner or 'its owner'} can list or change the listing of "
                  f"{repo or 'this app'}.")


def _slug_is_app_id(tree: list, slug: str) -> None:
    """Step 3: the listing is keyed by the app's identity. Its slug must be
    the `fused-app-id` its entry page declares at the listed commit, so one
    slug names one app for its whole life (a rename, a new repo name or a
    different app under a familiar name cannot take it over)."""
    entry = app_id.entry_of(tree)
    if entry is None:
        raise Fail(3, "The app has no entry page at that version (an .html file at its top "
                      "level with <meta name=\"fused-app\">), so its fused-app-id can't be read.")
    found = app_id.id_from_text(entry["text"])
    if found == slug:
        return
    if found is not None:
        raise Fail(3, f"The listing's slug must be the app's fused-app-id ({found}); "
                      f"this row says {slug}.")
    raw = app_id.raw_id(entry["text"])
    what = (f"{entry['path']} declares no fused-app-id" if raw is None else
            f"the fused-app-id in {entry['path']} ({raw[:70]}) isn't a valid id")
    raise Fail(3, f"The listing's slug must be the app's fused-app-id, but {what}. fused "
                  "stamps one when the app is created or first exported; publish from fused.")


def _same_repo_as_before(src, slug: str, repo: str, meta: dict) -> None:
    """A slug's releases all come from ONE GitHub repo. The review entries
    (the `reviews` branch keeps them after a delist too) record GitHub's
    numeric repo id; when the repo under the same name has a different id —
    an account renamed and its old login taken, a repo deleted and made
    again — the copies following this slug must not follow it."""
    rid = meta.get("id")
    if not isinstance(rid, int):
        return
    for e in src.review_entries(slug):
        prev = e.get("repo_id") if isinstance(e, dict) else None
        if isinstance(prev, int) and prev != rid:
            raise Fail(2, f"{slug} was listed from a different GitHub repository that had the "
                          f"name {repo}. A Fused maintainer has to look at this one.")


BLOCK_KEYS = ("slug", "commit", "level", "reason", "what_to_do", "at")


def blocklist_problems(text: str | None) -> list:
    """`blocklist.json` must be `{"blocked": [entry]}`, each entry exactly
    {slug, commit (40 hex), level hard|soft, reason, what_to_do, at}."""
    if text is None:
        return ["blocklist.json must stay in the index (an empty list is {\"blocked\": []})."]
    try:
        data = json.loads(text)
    except ValueError as e:
        return [f"blocklist.json isn't valid JSON ({e.msg}, line {e.lineno})."]
    if not isinstance(data, dict) or set(data) != {"blocked"} or not isinstance(data["blocked"], list):
        return ["blocklist.json must be an object with one key, blocked, holding a list."]
    out = []
    for i, b in enumerate(data["blocked"], 1):
        where = f"Block list entry {i}"
        if not isinstance(b, dict):
            out.append(f"{where} isn't an object.")
            continue
        if set(b) != set(BLOCK_KEYS):
            out.append(f"{where} must have exactly: " + ", ".join(BLOCK_KEYS) + ".")
            continue
        if not isinstance(b["slug"], str) or not SLUG_RE.match(b["slug"]):
            out.append(f"{where}: slug isn't a listing name.")
        if not isinstance(b["commit"], str) or not SHA_RE.match(b["commit"]):
            out.append(f"{where}: commit must be a full 40-character version id.")
        if b["level"] not in ("hard", "soft"):
            out.append(f"{where}: level must be hard or soft.")
        for k in ("reason", "what_to_do"):
            if not isinstance(b[k], str) or not b[k].strip():
                out.append(f"{where}: {k} must be a sentence.")
        if not isinstance(b["at"], str) or not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", b["at"]):
            out.append(f"{where}: at must be a time like 2026-10-27T09:00:00Z.")
    return out[:8]


def shape_problems(row: dict, slug: str, index: Index, status: str) -> list:
    keys = set(row)
    missing = [k for k in ROW_KEYS if k not in keys]
    extra = sorted(keys - set(ROW_KEYS))
    out: list = []
    if missing:
        out.append("The listing is missing " + ", ".join(missing) + ".")
    if extra:
        out.append("The listing has fields that aren't allowed: " + ", ".join(extra[:5]) + ".")
    if out:
        return out
    if row["slug"] != slug or not SLUG_RE.match(str(row["slug"])):
        out.append(f"The listing's slug must be {slug}, the same as its file name.")
    if not isinstance(row["branch"], str) or not re.match(r"^[\w./-]{1,255}$", row["branch"]):
        out.append("The listing's branch isn't a valid branch name.")
    if not isinstance(row["commit"], str) or not SHA_RE.match(row["commit"]):
        out.append("The listing's commit must be a full 40-character version id.")
    if not isinstance(row["name"], str) or not 1 <= len(row["name"].strip()) <= 60 \
            or "\n" in row["name"]:
        out.append("The name must be one line of 1 to 60 characters.")
    if not isinstance(row["description"], str) or not 1 <= len(row["description"].strip()) <= 400:
        out.append("The description must be 1 to 400 characters.")
    if row["category"] not in index.categories:
        out.append("The category must be one of: " + ", ".join(sorted(index.categories)) + ".")
    tags = row["tags"]
    if not isinstance(tags, list) or len(tags) > 8 or \
            not all(isinstance(t, str) and TAG_RE.match(t) for t in tags) or \
            len(set(tags)) != len(tags):
        out.append("Tags must be up to 8 different short lowercase words.")
    for key in ("icon", "preview"):
        v = row[key]
        if v is not None and not (isinstance(v, str) and ASSET_RE.match(v)):
            out.append(f"The {key} must be null or the path of an image inside the app.")
    rm = row["remix_of"]
    if rm is not None and (not isinstance(rm, str) or rm == slug or rm not in index.rows):
        out.append("remix_of must be null or the name of another listed app.")
    if not isinstance(row["version"], str) or not VERSION_RE.match(row["version"]):
        out.append("The version must look like 1.0.1.")
    if status == "added" and slug in index.rows:
        out.append(f"The name {slug} is already taken.")
    repo = str(row.get("repo", "")).lower()
    for other, orow in index.rows.items():
        if other != slug and str(orow.get("repo", "")).lower() == repo:
            out.append(f"This app is already listed as {other}.")
    return out


def hit_reason(h: dict) -> str:
    where = h.get("file")
    if where and h.get("line"):
        where = f"{where}, line {h['line']}"
    return f"{h['detail']} ({where})." if where else f"{h['detail']}."


# ------------------------------------------------------------ the comment


def block_json(result: dict) -> str:
    """The machine block's JSON. `<`, `>` and `&` are escaped so nothing
    inside can end the HTML comment."""
    block = {"sha": result["sha"], "status": result["status"],
             "reasons": result["reasons"], "whats_new": result["whats_new"],
             "new_powers": result["new_powers"]}
    if result.get("claude"):
        block["claude"] = result["claude"]
    text = json.dumps(block, ensure_ascii=False, separators=(", ", ": "))
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def parse_block(body: str) -> dict | None:
    m = BLOCK_RE.search(body or "")
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _md(text: str) -> str:
    """Untrusted text made inert for the human part of the comment: no
    mentions, no markup, no links."""
    text = " ".join(str(text).split())
    text = re.sub(r"([\\`*_{}\[\]()#+!|<>~])", r"\\\1", text)
    return text.replace("@", "@​")


def render_comment(result: dict, *, confirmed_by: str | None = None) -> str:
    lines = [f"<!-- fused-review {block_json(result)} -->"]
    st = result["status"]
    if st == "fail":
        step = result.get("step")
        name = f" at step {step}, {STEP_NAMES.get(step, '')}" if step else ""
        lines.append(f"### Review failed{name}")
        lines += [f"- {_md(r)}" for r in result["reasons"]] or ["- " + CRASH_REASON]
        lines.append("")
        lines.append("Fix the app, then update this listing from fused. The review "
                     "runs again on every update.")
    elif result.get("kind") == "maintainer":
        lines.append("### Maintainer change")
        lines.append("This changes the index itself. A maintainer reviews and merges it.")
    elif st == "needs-confirmation":
        lines.append("### Review passed: waiting for the author to confirm")
        lines.append("This release lets the app do new things:")
        lines += [f"- {_md(p['detail'])}" for p in result["new_powers"]]
        lines.append("")
        lines.append("The author confirms in fused (Confirm), or by commenting exactly:")
        lines.append(f"`fused: confirm {result['sha']}`")
    else:
        lines.append("### Review passed")
        if confirmed_by:
            lines.append(f"The author ({_md(confirmed_by)}) confirmed the new powers.")
        elif result.get("new_powers"):
            lines.append("This release lets the app do new things:")
            lines += [f"- {_md(p['detail'])}" for p in result["new_powers"]]
        lines.append("A Fused maintainer merges it; the app is listed once that lands.")
    if result.get("claude") == "skipped" and st != "fail":
        lines += ["", CLAUDE_OFF]
    if result.get("whats_new") and st != "fail":
        lines += ["", f"**What's new:** {_md(result['whats_new'])}"]
    return "\n".join(lines) + "\n"


def find_review_comment(gh: GitHub, repo: str, number: int):
    """The bot's own fused-review comment (never one a person wrote)."""
    for c in gh.issue_comments(repo, number):
        if (c.get("user") or {}).get("login") == BOT_LOGIN and \
                "<!-- fused-review " in (c.get("body") or ""):
            return c
    return None


def post(gh: GitHub, repo: str, number: int, body: str) -> None:
    c = find_review_comment(gh, repo, number)
    if c:
        gh.edit_comment(repo, c["id"], body)
    else:
        gh.create_comment(repo, number, body)


def set_label(gh: GitHub, repo: str, number: int, label: str | None) -> None:
    for name in LABELS:
        if name != label:
            gh.remove_label(repo, number, name)
    if label:
        gh.add_labels(repo, number, [label])


STATUS_TEXT = {"pass": "Review passed", "needs-confirmation":
               "Waiting for the author to confirm the new powers",
               "fail": "Review failed"}
#: The required `review` status per result. New powers keep it PENDING (so
#: the request cannot be merged) until the author's `fused: confirm`, which
#: confirm.py turns into success (D9).
STATUS_STATE = {"pass": "success", "needs-confirmation": "pending", "fail": "failure"}


def status_for(gh: GitHub, repo: str, sha: str, state: str, text: str) -> None:
    """The `review` commit status on the PR HEAD. A pull_request_target run
    belongs to the base branch's commit, so its own check run may not sit on
    the head; this status always does, and the ruleset requires it. Pending
    first, so a run that dies midway leaves the head blocked (M3)."""
    url = None
    if os.environ.get("GITHUB_RUN_ID"):
        url = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{repo}"
               f"/actions/runs/{os.environ['GITHUB_RUN_ID']}")
    try:
        gh.set_status(repo, sha, state, text, target_url=url)
    except GitHubError as e:
        print(f"could not set the review status: {e}", file=sys.stderr)


# -------------------------------------------------------------------- main


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    with open(os.environ["GITHUB_EVENT_PATH"], encoding="utf-8") as fh:
        event = json.load(fh)
    gh = GitHub()
    src = LiveSource(gh, repo, event)
    number = src.pr["number"]
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    head = src.pr["head_sha"]
    status_for(gh, repo, head, "pending", "Fused is reviewing this listing")
    try:
        result = run_steps(src, Index(root))
    except Exception as e:  # noqa: BLE001 — fail closed on anything
        print(f"review crashed: {type(e).__name__}: {e}", file=sys.stderr)
        result = {"sha": src.pr["head_sha"], "status": "fail", "reasons": [CRASH_REASON],
                  "whats_new": "", "new_powers": [], "step": None, "kind": None}
    print(json.dumps(result, indent=2))
    try:
        post(gh, repo, number, render_comment(result))
        label = {"pass": "review-passed", "fail": "review-failed",
                 "needs-confirmation": "needs-confirmation"}[result["status"]]
        set_label(gh, repo, number, None if result.get("kind") == "maintainer" else label)
    except GitHubError as e:
        print(f"could not post the result: {e}", file=sys.stderr)
        status_for(gh, repo, head, "failure", "The review could not post its result")
        return 1
    if ENFORCE:
        status_for(gh, repo, head, STATUS_STATE[result["status"]],
                   STATUS_TEXT[result["status"]])
    else:
        status_for(gh, repo, head, "success",
                   f"{STATUS_TEXT[result['status']]} (not enforced)")
    # No merge here: a maintainer merges by hand once the status is green.
    return 1 if result["status"] == "fail" and ENFORCE else 0


if __name__ == "__main__":
    sys.exit(main())
