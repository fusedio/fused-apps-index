"""Runs the review check against the deliberately bad (and a few good) pull
requests in `fixtures/` and exits non-zero on any mismatch.

Steps 1, 3 and 5 run for real (the PR's files, the index on disk, the app
tree from `fixtures/<name>/app/` in place of the tarball). Steps 2, 4, 6
and 7 run their real code against canned answers: `pr.json`'s `github`
section stands in for GitHub, its `model` list for the Anthropic API (the
real prompt is built and the real parser reads the canned reply).

A fixture is `fixtures/<name>/`:
    pr.json      {"author", "files": [{"filename", "status"}], "title"?, "body"?,
                  "github"?: {"users", "public_members", "maintainers",
                              "repos", "compare"},
                  "trees"?: {"<commit>": "<dir>"}, "model"?: [reply, …],
                  "env"?: {…}}
    apps/…       the PR's version of the row file (what `head_text` reads)
    base/apps/…  extra rows the base index has (besides the real apps/)
    app/         the app at the row's commit (other trees via "trees")
    expect.json  {"step": n, "reason_contains": "…"} for a fail, or
                 {"status": "pass" | "needs-confirmation", "kind"?,
                  "power_kinds"?, "block"?} for a pass

Also: self-checks of the comment block, the confirm rule, the safe tarball
reader and the diff, and — when FUSED_RENDER_SRC points at a fused-render
checkout — that rules.py is a verbatim copy of
fused_render/contrib/review_rules.py.

Every fixture runs with a dummy ANTHROPIC_API_KEY (so step 6 runs against
the canned replies) unless its `env` sets the key to "" (the no-key path).

Run: python3 -I scripts/review/run_fixtures.py   (no network, no secrets)
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import tarfile
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import check  # noqa: E402
import claude_review  # noqa: E402
import confirm  # noqa: E402
import fetch_app  # noqa: E402

ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
FIXTURES = os.path.join(ROOT, "fixtures")
DEFAULT_ANSWER = {"verdict": "pass", "reasons": [],
                  "whats_new": "A small app that does what its listing says.",
                  "new_powers": []}


def fake_sha(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


class FakeClient:
    """Stands in for `anthropic.Anthropic()`: each `messages.create` takes
    the next canned reply (the last one repeats)."""

    def __init__(self, replies: list):
        self.replies = replies or [{"answer": DEFAULT_ANSWER}]
        self.calls: list = []
        self.messages = self

    def with_options(self, **_kw):
        return self

    def create(self, **kw):
        self.calls.append(kw)
        reply = self.replies[min(len(self.calls), len(self.replies)) - 1]
        if "raise" in reply:
            raise type(reply["raise"], (Exception,), {})("canned failure")
        text = reply["text"] if "text" in reply else json.dumps(reply.get("answer"))
        return SimpleNamespace(stop_reason=reply.get("stop_reason", "end_turn"),
                               content=[SimpleNamespace(type="text", text=text)])


class FixtureSource:
    def __init__(self, name: str, spec: dict):
        self.dir = os.path.join(FIXTURES, name)
        self.spec = spec
        gh = spec.get("github") or {}
        self.users = gh.get("users") or {}
        self.members = gh.get("public_members") or {}
        self.maintainers = {m.lower() for m in gh.get("maintainers") or []}
        self.repos = gh.get("repos") or {}
        self.compare = gh.get("compare") or {}
        self.client = FakeClient(spec.get("model") or [])
        self.pr = {"number": 1, "author": spec["author"],
                   "head_sha": spec.get("head_sha") or fake_sha(name),
                   "head_repo": f"{spec['author']}/fused-apps-index",
                   "title": spec.get("title", ""), "body": spec.get("body", "")}

    def pr_files(self):
        return self.spec["files"]

    def head_text(self, path):
        full = os.path.join(self.dir, path)
        if not os.path.isfile(full):
            return None
        with open(full, encoding="utf-8") as fh:
            return fh.read()

    def user_type(self, login):
        return self.users.get(login, "User")

    def is_public_member(self, org, login):
        return login in (self.members.get(org) or [])

    def is_maintainer(self, login):
        return login.lower() in self.maintainers

    def repo_meta(self, repo):
        if repo in self.repos:
            return self.repos[repo]
        return {"default_branch": "main", "private": False}

    def compare_status(self, repo, base, head):
        return self.compare.get(f"{repo}@{head}", "behind")

    def app_tree(self, repo, sha):
        sub = (self.spec.get("trees") or {}).get(sha, "app")
        path = os.path.join(self.dir, sub)
        if not os.path.isdir(path):
            raise fetch_app.FetchError("The app's files could not be downloaded.")
        return fetch_app.files_from_dir(path)

    def review(self, **kw):
        return claude_review.review(client=self.client, **kw)


def base_rows(name: str) -> dict:
    rows = {}
    bdir = os.path.join(FIXTURES, name, "base", "apps")
    for f in sorted(os.listdir(bdir)) if os.path.isdir(bdir) else []:
        with open(os.path.join(bdir, f), encoding="utf-8") as fh:
            rows[f[:-5]] = json.load(fh)
    return rows


def run_fixture(name: str) -> tuple:
    d = os.path.join(FIXTURES, name)
    with open(os.path.join(d, "pr.json"), encoding="utf-8") as fh:
        spec = json.load(fh)
    with open(os.path.join(d, "expect.json"), encoding="utf-8") as fh:
        expect = json.load(fh)
    # Claude's review runs (against the canned replies) unless a fixture
    # sets ANTHROPIC_API_KEY to "" to test the no-key path.
    env = dict({"ANTHROPIC_API_KEY": "fixture-dummy-key"}, **(spec.get("env") or {}))
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        src = FixtureSource(name, spec)
        result = check.run_steps(src, check.Index(ROOT, base_rows(name)))
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    problems = []
    if "step" in expect:
        if result["status"] != "fail" or result["step"] != expect["step"]:
            problems.append(f"expected a fail at step {expect['step']}, got "
                            f"{result['status']} at step {result['step']}")
        want = expect.get("reason_contains", "").lower()
        if want and not any(want in r.lower() for r in result["reasons"]):
            problems.append(f"no reason contains {expect['reason_contains']!r}: "
                            f"{result['reasons']}")
    else:
        if result["status"] != expect["status"]:
            problems.append(f"expected {expect['status']}, got {result['status']} "
                            f"(step {result['step']}: {result['reasons']})")
        if "kind" in expect and result["kind"] != expect["kind"]:
            problems.append(f"expected kind {expect['kind']}, got {result['kind']}")
        if "power_kinds" in expect:
            kinds = sorted({p["kind"] for p in result["new_powers"]})
            if kinds != sorted(expect["power_kinds"]):
                problems.append(f"expected powers {expect['power_kinds']}, got {kinds}")
        if "block" in expect:
            block = check.parse_block(check.render_comment(result))
            want = dict(expect["block"], sha=src.pr["head_sha"])
            if block != want:
                problems.append(f"block {block} != {want}")
    return result, problems


# -------------------------------------------------------------- self-checks


def self_checks() -> list:
    bad = []
    # The comment block survives hostile text and parses back exactly.
    hostile = {"sha": "a" * 40, "status": "fail", "step": 6, "kind": None,
               "reasons": ["ends --> here <!-- and & stuff", "@someone ping"],
               "whats_new": "", "new_powers": []}
    body = check.render_comment(hostile)
    block = check.parse_block(body)
    if block != {k: hostile[k] for k in ("sha", "status", "reasons", "whats_new",
                                          "new_powers")}:
        bad.append(f"block round trip: {block}")
    if body.split("\n", 1)[0].count("-->") != 1:
        bad.append("block: '-->' inside the JSON")
    if "@someone" in body.split("\n", 1)[1]:
        bad.append("human text keeps a live @mention")
    # The no-key block names the skip; the human text says so.
    off = dict(hostile, status="pass", step=None, reasons=[], claude="skipped",
               whats_new="Adds a dark theme.")
    body = check.render_comment(off)
    if (check.parse_block(body) or {}).get("claude") != "skipped" or \
            check.CLAUDE_OFF not in body:
        bad.append("no-key comment lacks the skip marker")
    for title, text, want in [
            ("Update Notes", "## What changed\nAdds a dark theme.\n\nMore.", "Adds a dark theme."),
            ("Update Notes", "What changed: Faster saving.", "Faster saving."),
            ("Update Notes", "Counts words now.\nSecond line.", "Counts words now."),
            ("List Notes", "", "List Notes")]:
        got = check.fallback_whats_new({"title": title, "body": text})
        if got != want:
            bad.append(f"fallback_whats_new({text!r}) = {got!r}, want {want!r}")
    # A comment a person wrote is never the review.
    if check.parse_block("no block") is not None:
        bad.append("parse_block invents a block")

    # The confirm rule.
    sha = "b" * 40
    good_bot = {"body": check.render_comment({"sha": sha, "status": "needs-confirmation",
                                             "reasons": [], "whats_new": "x",
                                             "new_powers": [{"kind": "network",
                                                             "detail": "d"}]})}
    pr = {"state": "open", "user": {"login": "alice"}, "head": {"sha": sha}}
    ev = {"issue": {"pull_request": {}}, "comment": {"body": f"fused: confirm {sha}",
                                                     "user": {"login": "Alice"}}}
    cases = [
        (ev, pr, good_bot, True),
        (dict(ev, comment={"body": f"fused: confirm {sha}", "user": {"login": "bob"}}),
         pr, good_bot, False),
        (ev, dict(pr, head={"sha": "c" * 40}), good_bot, False),
        (ev, dict(pr, state="closed"), good_bot, False),
        (ev, pr, {"body": good_bot["body"].replace("needs-confirmation", "fail")}, False),
        (ev, pr, None, False),
        (dict(ev, comment={"body": f"fused: confirm {sha} please", "user": {"login": "alice"}}),
         pr, good_bot, False),
    ]
    for i, (e, p, b, want) in enumerate(cases):
        if confirm.decide(e, p, b)[0] is not want:
            bad.append(f"confirm case {i}: expected {want}")

    # The tarball reader: links recorded, unsafe names refused.
    def tgz(members):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            for name, kind, data in members:
                ti = tarfile.TarInfo(name)
                if kind == "sym":
                    ti.type, ti.linkname = tarfile.SYMTYPE, "/etc/passwd"
                    t.addfile(ti)
                else:
                    ti.size = len(data)
                    t.addfile(ti, io.BytesIO(data))
        return buf.getvalue()

    tree = fetch_app.read_tarball(tgz([("r-1/a.py", "f", b"x = 1\n"),
                                       ("r-1/link", "sym", b"")]))
    if [(e["path"], e["mode"]) for e in tree] != [("a.py", "100644"), ("link", "120000")]:
        bad.append(f"tarball: {tree}")
    for evil in ("r-1/../../x", "/abs/x", "r-1/a\\b"):
        try:
            fetch_app.read_tarball(tgz([(evil, "f", b"x")]))
            bad.append(f"tarball accepted {evil!r}")
        except fetch_app.FetchError:
            pass

    # The diff: a changed line and a new file.
    d = fetch_app.diff_trees([{"path": "a.py", "text": "x = 1", "mode": "100644"}],
                             [{"path": "a.py", "text": "x = 2", "mode": "100644"},
                              {"path": "b.py", "text": "y", "mode": "100644"}])
    for want in ("-x = 1", "+x = 2", "new file mode 100644", "+++ b/b.py"):
        if want not in d:
            bad.append(f"diff lacks {want!r}")

    # The model answer: strict.
    if claude_review.parse_answer("Sure! {\"verdict\": \"pass\"}") is not None:
        bad.append("parse_answer accepts prose")
    if claude_review.parse_answer(json.dumps({"verdict": "fail", "reasons": [],
                                              "whats_new": "x", "new_powers": []})):
        bad.append("parse_answer accepts a fail without reasons")
    return bad


def rules_copy_check() -> str | None:
    src = os.environ.get("FUSED_RENDER_SRC")
    if not src:
        return None
    upstream = os.path.join(src, "fused_render", "contrib", "review_rules.py")
    with open(upstream, "rb") as a, open(os.path.join(HERE, "rules.py"), "rb") as b:
        if a.read() != b.read():
            return f"rules.py differs from {upstream}: copy it over verbatim"
    return "ok"


def main() -> int:
    failures = 0
    names = sorted(n for n in os.listdir(FIXTURES)
                   if os.path.isfile(os.path.join(FIXTURES, n, "expect.json")))
    for name in names:
        try:
            result, problems = run_fixture(name)
        except Exception as e:  # noqa: BLE001 — a crash is a mismatch
            result, problems = None, [f"crashed: {type(e).__name__}: {e}"]
        if problems:
            failures += 1
            print(f"FAIL  {name}")
            for p in problems:
                print(f"      {p}")
        else:
            what = (f"step {result['step']}: {result['reasons'][0]}"
                    if result["status"] == "fail" else
                    f"{result['status']} ({result['kind']})")
            print(f"ok    {name:24} {what}")
    for p in self_checks():
        failures += 1
        print(f"FAIL  self-check: {p}")
    copy = rules_copy_check()
    if copy is None:
        print("skip  rules.py copy check (set FUSED_RENDER_SRC to a fused-render checkout)")
    elif copy != "ok":
        failures += 1
        print(f"FAIL  {copy}")
    else:
        print("ok    rules.py is a verbatim copy of review_rules.py")
    print(f"\n{len(names)} fixtures, {failures} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
