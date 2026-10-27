"""An app's files at one commit, read as DATA (§18.2 step 5).

The tarball comes from codeload (`codeload.github.com/<repo>/tar.gz/<sha>`)
and is read in memory: nothing is written to disk, nothing in it is run.
Only regular files are kept, with their bytes; a link (symbolic or hard) is
kept as an entry with mode "120000" and no content so the fixed rules fail
it plainly instead of it vanishing; any other member type, an absolute
path, a `..`, a backslash, or a size past the caps is a `FetchError`.

A binary file's entry also carries `rules.sniff` evidence (`head`, the
first bytes, and `zip`, whether a zip signature sits anywhere in it): the
fixed rules judge a picture by its bytes, and fail one that has none. A
file past MAX_FILE_BYTES is streamed once for that evidence and not kept.

`files_from_dir` reads a fixture's `app/` folder into the same shape, and
`diff_trees` turns two trees into a git-style unified diff, which is what
Claude's review reads for an update and what `rules.powers` scans.
"""

from __future__ import annotations

import difflib
import hashlib
import io
import os
import re
import stat
import tarfile
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rules  # noqa: E402

CODELOAD = "https://codeload.github.com"
TIMEOUT_S = 60
MAX_ARCHIVE_BYTES = 60 * 1024 * 1024      # compressed
MAX_TOTAL_BYTES = 150 * 1024 * 1024       # uncompressed
MAX_FILE_BYTES = 8 * 1024 * 1024          # one file's bytes kept for checking
MAX_FILES = 3000
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")


class FetchError(RuntimeError):
    """A plain one-line reason the app could not be read."""


def fetch_tree(repo: str, sha: str, *, token: str | None = None) -> list:
    """The app at `sha` as `[{"path", "text", "mode", "binary", "size"}]`."""
    if not _REPO.match(repo or "") or not _SHA.match(sha or ""):
        raise FetchError("The app's address or version is not in the expected form.")
    req = urllib.request.Request(f"{CODELOAD}/{repo}/tar.gz/{sha}")
    req.add_header("User-Agent", "fused-apps-index-review")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read(MAX_ARCHIVE_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise FetchError(f"The app's files could not be downloaded (GitHub answered "
                         f"{e.code}).") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError("The app's files could not be downloaded.") from e
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise FetchError("The app is too large to review (over 60 MB compressed).")
    return read_tarball(raw)


def read_tarball(raw: bytes) -> list:
    out: list = []
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            for m in tar:
                if m.type in (tarfile.XGLTYPE, tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME,
                              tarfile.GNUTYPE_LONGLINK):
                    continue
                path = _inner_path(m.name)
                if path is None:
                    continue  # the archive's top folder itself
                if m.isdir():
                    continue
                if len(out) >= MAX_FILES:
                    raise FetchError(f"The app has more than {MAX_FILES} files, too many "
                                     "to review.")
                if m.issym() or m.islnk():
                    out.append({"path": path, "text": None, "mode": "120000",
                                "binary": False, "size": 0})
                    continue
                if not m.isreg():
                    raise FetchError(f"The app contains a special file ({path}) that "
                                     "is not a plain file.")
                total += m.size
                if total > MAX_TOTAL_BYTES:
                    raise FetchError("The app is too large to review (over 150 MB).")
                mode = "100755" if m.mode & 0o111 else "100644"
                if m.size > MAX_FILE_BYTES:
                    out.append({"path": path, "text": None, "mode": mode,
                                "binary": True, "size": m.size,
                                **_stream_sniff(tar.extractfile(m))})
                    continue
                fh = tar.extractfile(m)
                data = fh.read() if fh is not None else b""
                out.append(_entry(path, data, mode))
    except tarfile.TarError as e:
        raise FetchError("The app's files could not be read.") from e
    out.sort(key=lambda e: e["path"])
    return out


def _inner_path(name: str) -> str | None:
    """`<repo>-<sha>/a/b` → `a/b`; None for the top folder. Unsafe names
    are a FetchError, never skipped silently."""
    if "\\" in name or "\0" in name or name.startswith("/"):
        raise FetchError("The app contains a file with an unsafe name.")
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise FetchError("The app contains a file with an unsafe name.")
    if len(parts) <= 1:
        return None
    return "/".join(parts[1:])


def _entry(path: str, data: bytes, mode: str) -> dict:
    text = None
    if b"\0" not in data:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
    out = {"path": path, "text": text, "mode": mode, "binary": text is None,
           "size": len(data), "sha": hashlib.sha256(data).hexdigest()}
    if text is None:
        out.update(rules.sniff(data))
    return out


def _stream_sniff(fh) -> dict:
    """`rules.sniff`'s evidence for a file too big to keep, read in chunks
    (a signature split across two chunks is still found)."""
    if fh is None:
        return {}
    head, tail, found = b"", b"", False
    sigs = (b"PK\x03\x04", b"PK\x05\x06")
    while True:
        chunk = fh.read(1024 * 1024)
        if not chunk:
            break
        if len(head) < 64:
            head = (head + chunk)[:64]
        window = tail + chunk
        found = found or any(sg in window for sg in sigs)
        tail = window[-3:]
    return {"head": head, "zip": found}


def files_from_dir(root: str) -> list:
    """A fixture's app folder in `fetch_tree`'s shape. Links are recorded,
    never followed."""
    out: list = []
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in dirnames + sorted(filenames):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            st = os.lstat(full)
            if stat.S_ISLNK(st.st_mode):
                out.append({"path": rel, "text": None, "mode": "120000",
                            "binary": False, "size": 0})
            elif stat.S_ISREG(st.st_mode):
                with open(full, "rb") as fh:
                    data = fh.read()
                out.append(_entry(rel, data, "100755" if st.st_mode & 0o111 else "100644"))
    out.sort(key=lambda e: e["path"])
    return out


def diff_trees(old: list, new: list) -> str:
    """A git-style unified diff from tree `old` to tree `new` (3 lines of
    context). Binary or unreadable files show as "Binary files … differ"."""
    a = {e["path"]: e for e in old or []}
    b = {e["path"]: e for e in new or []}
    chunks: list = []
    for path in sorted(set(a) | set(b)):
        ea, eb = a.get(path), b.get(path)
        if ea and eb and ea.get("text") == eb.get("text") and ea.get("mode") == eb.get("mode") \
                and ea.get("size") == eb.get("size") and ea.get("text") is not None:
            continue
        if ea and eb and ea.get("text") is None and eb.get("text") is None and \
                ea.get("size") == eb.get("size") and ea.get("mode") == eb.get("mode") and \
                ea.get("sha") == eb.get("sha") and ea.get("sha") is not None:
            continue
        head = [f"diff --git a/{path} b/{path}"]
        if ea is None:
            head.append(f"new file mode {eb.get('mode') or '100644'}")
        elif eb is None:
            head.append(f"deleted file mode {ea.get('mode') or '100644'}")
        elif ea.get("mode") != eb.get("mode"):
            head += [f"old mode {ea.get('mode')}", f"new mode {eb.get('mode')}"]
        ta = ea.get("text") if ea else ""
        tb = eb.get("text") if eb else ""
        if ta is None or tb is None:
            head.append(f"Binary files {'a/' + path if ea else '/dev/null'} and "
                        f"{'b/' + path if eb else '/dev/null'} differ")
            chunks.append("\n".join(head))
            continue
        body = list(difflib.unified_diff(
            ta.splitlines(), tb.splitlines(),
            fromfile=f"a/{path}" if ea else "/dev/null",
            tofile=f"b/{path}" if eb else "/dev/null", n=3, lineterm=""))
        chunks.append("\n".join(head + body))
    return "\n".join(chunks) + ("\n" if chunks else "")
