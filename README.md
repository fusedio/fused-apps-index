# Fused Marketplace index

This repo is the list of apps in the Fused Marketplace. It holds **pointers,
not code**: each listed app is one small file naming the author's own
GitHub repo and the exact version of it that passed review. When someone
installs an app from the Marketplace, fused copies the author's repo at that
version, and only ever moves the copy to another version that passed review.

## How an app gets listed

From fused, not by hand. In the app's page, **Publish to marketplace** opens
a pull request here that adds `apps/<slug>.json`; **Publish update** opens one
that moves its `commit`; **Remove from marketplace** opens one that deletes
it. Every such pull request runs the `review` check:

1. **Files.** Only one `apps/<slug>.json` changes.
2. **Who.** The sender owns the app's repo (or is a *public* member of the
   organization that does). Private org membership can't be seen by the check;
   make it public to list an org's app.
3. **Shape.** The listing has exactly the fields below, a free name, a repo
   not listed under another name, and follows the repo's main branch.
4. **Which commit.** The version is on the author's main branch (a version
   that exists only in someone else's copy is refused).
5. **Fixed rules.** The app at that version has none of: code downloaded and
   run while it runs, scrambled or long encoded text, invisible characters,
   ready-made program files, links, install-time hooks, reading saved
   passwords or keys, a committed `.fused/` folder, text addressed to an AI
   reviewer. Nothing overrules these.
6. **Claude's review.** The whole app on its first listing, the change since
   the last approved version after that. An error or a timeout is a fail.
   *For now no Anthropic key is set*, so this step is skipped (not failed):
   the comment says "Claude's review is off (no key set); only the fixed rules
   ran.", the block carries `"claude": "skipped"`, What's new is taken from the
   request's "What changed" text (else its first line, else its title), and new
   powers come from the fixed scan alone. Setting the key turns the step on.
7. **New powers.** If a release lets the app do something new (talk to a new
   place on the internet, touch files outside its folder, install packages,
   run in the background, give Claude tools), the check passes but waits for
   the author to confirm in fused (it comments `fused: confirm <sha>`).
8. **Result.** One comment on the pull request, with a machine-readable
   `<!-- fused-review {...} -->` block fused reads. A clean pass merges on
   its own.

Then, every 10 minutes, a job writes the release's plain-language "What's
new" into `reviews/<slug>.json` on the **`reviews` branch**. `main` is fully
protected (no bypass, admins included), so the bot never writes it; the
`reviews/` folder on `main` holds hand-written entries and is the fallback
for a slug the branch doesn't have yet. Readers take the branch first.

## Files

| Path | What | Who changes it |
|---|---|---|
| `apps/<slug>.json` | One listed app (the row) | Authors, through fused, via the review check |
| `reviews/<slug>.json` on the `reviews` branch | Each approved release's What's new, newest first | The review bot only, after a merge (plain push; the branch holds only `reviews/`) |
| `reviews/<slug>.json` on `main` | Hand entries (the fallback) | Fused maintainers |
| `blocklist.json` | Releases pulled after approval (`hard` stops the app, `soft` pauses updates) | Fused maintainers, human-reviewed PRs |
| `collections.json` | Featured apps and shelves | Fused maintainers |
| `categories.json` | The Marketplace's categories | Fused maintainers |
| `scripts/review/` | The check (Python 3.12, stdlib + `anthropic`) | Fused maintainers |
| `fixtures/` | Deliberately bad (and a few good) pull requests the check must judge right | Fused maintainers |

A row:

```json
{"slug": "sine-starter", "repo": "vasu2001/sine-starter", "branch": "main",
 "commit": "<40 hex>", "name": "Sine Starter", "description": "…",
 "category": "starters", "tags": ["starter"], "icon": null,
 "preview": "preview.png", "remix_of": null, "version": "1.0.1"}
```

Every key is required and no others are allowed. `slug` is the file name
(`[a-z0-9][a-z0-9-]{1,63}`); `icon` and `preview` are image paths inside the
app's repo at `commit`, or null; `remix_of` is another listed slug, or null.

A block-list entry: `{"slug", "commit", "level": "hard" | "soft", "reason",
"what_to_do", "at"}`.

## For maintainers

- Changes to anything but one row are maintainer changes: the check passes
  them for index admins and maintainers ("a person reviews and merges this")
  and never merges them on its own. Anyone else's fail.
- `python3 -I scripts/review/run_fixtures.py` runs every fixture (no network,
  no secrets). With `FUSED_RENDER_SRC=<fused-render checkout>` it also checks
  that `scripts/review/rules.py` is a verbatim copy of
  `fused_render/contrib/review_rules.py`. Change the rules there, then copy.
- One-time setup: `scripts/setup_ruleset.sh` (ruleset, auto-merge, labels),
  then, when the owner is ready to turn Claude's review on,
  `gh secret set ANTHROPIC_API_KEY`. Without it every non-AI step still runs
  and clean listings still merge. The required check `review` is a commit
  status the check sets on the pull request's head.
- The check never runs code from a pull request or from an app: both are
  read as data. Keep it that way.
