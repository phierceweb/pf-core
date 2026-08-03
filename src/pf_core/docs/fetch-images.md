# Fetch: image localizer

Download the remote image refs in a markdown (or HTML-derived markdown) document into a local `images/` directory and retarget the refs — so converted documents stay self-contained after the source URLs expire, and downstream passes that only read local files can see every figure. Built on [`fetch`](fetch.md); stdlib-only, base install.

---

## Table of Contents

- [Quick usage](#quick-usage)
- [What gets localized](#what-gets-localized)
- [Naming and reuse](#naming-and-reuse)
- [Resumable file mode](#resumable-file-mode)
- [Failure containment](#failure-containment)
- [Injection seams](#injection-seams)

## Quick usage

```python
from pf_core.fetch.images import localize_images, localize_file, count_remote_images

# Text in, text out — for pipelines that hold the document in memory
result = localize_images(markdown, out_dir / "images", base_url="https://example.com/help/")
markdown = result.markdown            # refs retargeted to images/<name>
# result.saved: list[Path] written or reused; result.failed: count left remote

# File-as-ledger mode — for large documents localized across multiple runs.
# Stop when a pass localizes nothing: some refs fail permanently (404, or a
# body that isn't an image), so looping on count_remote_images never exits.
while localize_file(doc_path, doc_path.parent / "images"):
    pass
```

## What gets localized

- Markdown image refs `![alt](https://…)` and HTML `<img src="https://…">`.
- **Relative refs** (`../assets/figure.png`) only when `base_url` is given — they resolve via `urljoin`. Without `base_url` they are left for the browser.
- Refs already under `images/`, absolute filesystem paths, and `data:` URIs are never touched.
- A ref with a **non-image extension** is skipped; an image extension or **no extension at all** qualifies (opaque CDN URLs) — extensionless downloads get their extension sniffed from magic bytes (`sniff_image_ext`; png/jpg/gif/webp/svg/avif/heic/bmp/tiff/ico/jxl).
- **The body must actually be an image.** `sniff_image_ext` returns `None` for anything it doesn't recognize — an HTML interstitial, a JSON error, a login page — and that is a per-URL failure: the ref stays remote and counts in `failed`. This applies to extensioned URLs too, so a CDN path ending `.png` that 200s with a sign-in wall is not written to disk and reported as saved. An XML body counts as SVG only when `<svg` appears within the first 4 KiB, which clears a DOCTYPE and an embedded license header but not an arbitrarily long preamble.

Rewrites are anchor-safe — `]({url})` and `src="{url}"` forms only — so a bare URL in prose is never rewritten. Duplicate refs are fetched once and retargeted everywhere.

## Naming and reuse

`default_namer` derives a deterministic, collision-resistant local name by dash-joining the URL path segments after the first `images` component (`…/images/getting-started/interface.png` → `getting-started-interface.png`), falling back to the last two segments. Deterministic names are what make `reuse_existing=True` (the default) safe: a re-run probes `images_dir` for the name and reuses the file without refetching.

**Names are persisted artifacts** — they land in committed documents and on-disk trees. Treat `default_namer`'s output like [`slugify`](slugify.md)'s: changing the scheme breaks reuse against every existing output directory. A project migrating from its own namer passes it in:

```python
localize_images(md, images_dir, namer=my_legacy_namer, reuse_existing=False)
```

With `reuse_existing=False`, existing files are never probed; new names take `-2`, `-3`… suffixes against the directory's current listing — the mode for basename-style namers whose collisions are real.

## Resumable file mode

`localize_file` makes the document itself the progress ledger: each ref is retargeted in memory the moment its image lands, and the document is checkpointed every `checkpoint_every` images (default 50) plus once at the end. A run killed partway keeps everything it localized — finished refs point at `images/`, pending refs stay remote — so re-running converges without re-downloading. All writes (images and checkpoints) are atomic ([`io`](io.md)).

## Failure containment

A per-URL failure — HTTP error, network error, SSRF block, size-cap `ClientError`, corrupt or truncated body, non-image response body, disk error — counts in `failed`, logs a warning, **keeps the remote ref** (it still resolves in a browser), and the loop continues. `localize_images` never raises for an individual URL; it raises only for programmer errors (bad arguments, missing parent directories).

## Injection seams

- `fetcher=` — any object with `get_bytes(url, *, timeout_s=…)`. The default is a `Fetcher()` with the module's size cap; pass a configured [`Fetcher`](fetch.md) for pacing/UA/limits, or a fake in tests.
- `namer=` — the naming function, as above.
- `link_prefix=` — the retarget prefix (default `images/`).

Wrap this module rather than forking it: keep project-specific env toggles and defaults in your wrapper and pass them here as explicit arguments.
