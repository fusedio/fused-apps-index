"""The app's identity, read from its entry page at the listed commit.

A listing's slug IS the app's `fused-app-id`: the value of
`<meta name="fused-app-id" content="<kebab>-<8hex>">` in the app's entry
page, minted once when the app is made and never changed after. Keying the
index by it means a listing names one app for its whole life, whatever the
repo or the app is called.

COPIES, to the letter, of two fused-render rules (this repo imports nothing
from fused-render; `rules.py` is the one verbatim file and must stay so):

- `fused_render/app_id.py`: `ID_RE` and `id_from_text` (the tag is found
  first, then `content` is read out of THAT tag, in the first 4 KiB; a value
  that fails `ID_RE` is absent).
- `fused_render/app_listing.py::app_entry`: the entry page is the FIRST
  non-hidden direct-child `.html` (name order, case-insensitive suffix)
  carrying `<meta name="fused-app">` in its first 4 KiB.
"""

from __future__ import annotations

import re

_META_SCAN_BYTES = 4096
_HEX_LEN = 8
_NAME_MAX = 48
ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,%d})-[0-9a-f]{%d}$" % (_NAME_MAX - 1, _HEX_LEN))
_TAG_RE = re.compile(
    rb"<meta\s[^>]*name\s*=\s*[\"']?fused-app-id[\"']?[^>]*>", re.IGNORECASE)
_CONTENT_RE = re.compile(rb"content\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)
_FUSED_META_RE = re.compile(
    # The value must END there (closing quote, or unquoted then a space,
    # `/` or `>`): `name="fused-app-id"` is the app id tag, not the marker.
    rb"<meta\s[^>]*name\s*=\s*(?:\"fused-app\"|'fused-app'|fused-app(?=[\s/>]))", re.IGNORECASE)


def is_valid(value: object) -> bool:
    return isinstance(value, str) and ID_RE.match(value) is not None


def _head(text: str | bytes) -> bytes:
    if isinstance(text, str):
        text = text.encode("utf-8", "ignore")
    return text[:_META_SCAN_BYTES]


def id_from_text(head: str | bytes) -> str | None:
    """The declared app id in a page's head; None when the tag is absent or
    its content is not a well-formed id."""
    tag = _TAG_RE.search(_head(head))
    if not tag:
        return None
    m = _CONTENT_RE.search(tag.group(0))
    if not m:
        return None
    value = m.group(1).decode("utf-8", "ignore").strip()
    return value if is_valid(value) else None


def entry_of(tree: list) -> dict | None:
    """The app's entry page among `fetch_app` tree entries, or None."""
    for e in sorted(tree, key=lambda e: e.get("path", "")):
        path = e.get("path") or ""
        if "/" in path or path.startswith(".") or not path.lower().endswith(".html"):
            continue
        if e.get("mode") == "120000" or not isinstance(e.get("text"), str):
            continue
        if _FUSED_META_RE.search(_head(e["text"])):
            return e
    return None


def raw_id(text: str | bytes) -> str | None:
    """The fused-app-id tag's raw content (well-formed or not), for a plain
    refusal message; None when there is no tag."""
    tag = _TAG_RE.search(_head(text))
    m = _CONTENT_RE.search(tag.group(0)) if tag else None
    return m.group(1).decode("utf-8", "ignore").strip() if m else None
