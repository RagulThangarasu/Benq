#!/usr/bin/env python3
"""Download the generated BenQ HTML (AEM Sites pages) for every product.

AEM Guides publishes each map as a tree of pages under:

    /content/guide/<category>/<type>/<PRODUCT>/<lang>/<topic pages…>

This crawls every folder under the shipped guide categories (zowie, business,
consumer, infty, education), keeps the pages that sit under a wanted language
folder (`en` by default), and saves each page's rendered HTML to:

    benq_html/<PRODUCT>/index.html          (the language landing page)
    benq_html/<PRODUCT>/<topic>.html        (one file per topic page)

Other languages land in `benq_html/<PRODUCT>__<lang>/`.

Run:  python download_benq_html.py
"""
import json
import os
import re
import urllib.parse

# Reuse one AEM client: same host, credentials, retries and reachability check
# as the PDF downloader, so both pages fail in the same plain language.
from download_benq_pdfs import (  # noqa: F401  (ServerUnreachable is re-exported)
    HOST,
    ServerUnreachable,
    _clean_folder,
    _get,
    check_server,
    wanted_categories as _wanted_categories,
)

# Root of the published guide pages; configurable for other environments.
GUIDE_ROOT = os.environ.get("BENQ_AEM_GUIDE_ROOT", "/content/guide").rstrip("/")
# Which category folders to crawl comes from BENQ_AEM_GUIDE_CATEGORIES, shared
# with the PDF downloader so one setting governs both.
# Comma-separated language codes to pull, or "*" for every language present.
HTML_LANGS = os.environ.get("BENQ_AEM_HTML_LANGS", "en")
OUT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "benq_html")

# Language folders look like `en`, `zh-cn`, `ar-me` — never a topic page name.
_LANG_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2,3})?$")
# AEM bookkeeping nodes that render nothing useful.
_SKIP_SEGMENTS = {"__references__", "jcr:content"}


def _wanted_langs():
    if HTML_LANGS.strip() == "*":
        return None
    return {s.strip().lower() for s in HTML_LANGS.split(",") if s.strip()}


def _split_page(path):
    """`/content/guide/benq/monitor/g85t/en/topic-2` -> ('g85t', 'en', 'topic-2', …).

    Returns (product, lang, relative, product_path) or None when the page does
    not sit under a language folder (category landing pages, search, and so on).
    """
    parts = [p for p in path.strip("/").split("/") if p]
    for i, seg in enumerate(parts):
        if not _LANG_RE.match(seg) or i == 0:
            continue
        rel = parts[i + 1:]
        if any(r in _SKIP_SEGMENTS for r in rel):
            return None
        return (parts[i - 1], seg.lower(), "/".join(rel),
                "/" + "/".join(parts[:i]))
    return None


def _disambiguate(entries):
    """Folder name per product, keeping same-named products apart.

    The same product name appears under more than one category (`1-en` lives
    under both `projector` and `monitor-accessory`, `sw272` under two different
    `monitor` folders); left alone they would share a folder and overwrite each
    other's pages. Only the clashing ones get their full category path prefixed,
    so ordinary products keep a clean folder name.
    """
    paths_by_name = {}
    for product, _lang, _p, _rel, product_path in entries:
        paths_by_name.setdefault(product, set()).add(product_path)
    root = GUIDE_ROOT.strip("/").split("/")
    folders = {}
    for name, paths in paths_by_name.items():
        for path in paths:
            if len(paths) == 1:
                folders[path] = name
                continue
            parts = path.strip("/").split("/")
            if parts[:len(root)] == root:
                parts = parts[len(root):]
            folders[path] = "_".join(parts)
    return folders


def discover_guide_pages():
    """Every page in a wanted category that lives in a wanted language folder.

    Returns a sorted list of (folder, lang, page_path, relative_path). Within the
    wanted categories this is a full crawl — no product allow-list — so every
    folder with a language subtree is included.
    """
    # p.guessTotal=true is required: without it querybuilder stops counting (and
    # returning) hits early, so a deep tree yields only the first few pages.
    raw = _get(f"/bin/querybuilder.json?path={GUIDE_ROOT}&type=cq:Page"
               f"&p.limit=-1&p.guessTotal=true"
               f"&p.hits=selective&p.properties=jcr:path")
    hits = json.loads(raw).get("hits", [])
    wanted = _wanted_langs()
    categories = _wanted_categories()
    root_depth = len([x for x in GUIDE_ROOT.strip("/").split("/") if x])
    seen, out = set(), []
    for h in hits:
        p = h.get("jcr:path")
        if not p or p in seen:
            continue
        seen.add(p)
        if categories is not None:
            parts = [x for x in p.strip("/").split("/") if x]
            if (len(parts) <= root_depth
                    or parts[root_depth].lower() not in categories):
                continue
        split = _split_page(p)
        if not split:
            continue
        product, lang, rel, product_path = split
        if wanted is not None and lang not in wanted:
            continue
        out.append((product, lang, p, rel, product_path))
    folders = _disambiguate(out)
    out = [(folders[product_path], lang, p, rel)
           for _product, lang, p, rel, product_path in out]
    out.sort(key=lambda t: (t[0].lower(), t[1], t[3]))
    return out


def _dest_for(product, lang, rel):
    """Local file for one page: the language root becomes `index.html`."""
    folder = product if lang == "en" else f"{product}__{lang}"
    parts = [_clean_folder(folder)]
    parts += [_clean_folder(s) for s in rel.split("/") if s]
    if len(parts) == 1:
        parts.append("index")
    return os.path.join(OUT_ROOT, *parts) + ".html"


def download_all(progress_cb=None, should_cancel=None):
    """Crawl every folder under GUIDE_ROOT and download EVERY page's HTML.

    progress_cb(frac, msg): optional callback (frac in 0..1) for the web UI.
    should_cancel(): optional predicate polled between pages; when it returns
    true the crawl stops where it is and reports what it already downloaded.
    Returns {ok, total, out_root, cancelled, rows:[{product, status, ...}]}.
    Each row's status is one of: ok | error | bad_html.
    """
    def _emit(frac, msg=""):
        if progress_cb:
            try:
                progress_cb(max(0.0, min(1.0, float(frac))), msg)
            except Exception:
                pass

    def _cancelled():
        if not should_cancel:
            return False
        try:
            return bool(should_cancel())
        except Exception:
            return False

    _emit(0.01, f"checking {HOST}…")
    check_server()
    _emit(0.02, "crawling every folder for generated HTML…")
    pages = discover_guide_pages()

    os.makedirs(OUT_ROOT, exist_ok=True)
    rows, ok = [], 0
    total = len(pages)
    cancelled = False
    for i, (product, lang, page_path, rel) in enumerate(pages):
        if _cancelled():
            cancelled = True
            print(f"  [CANCELLED]  stopped after {i} of {total}")
            break
        label = product if lang == "en" else f"{product} ({lang})"
        name = f"{label}/{rel or 'index'}"
        _emit(0.05 + 0.93 * i / max(total, 1), f"{name} ({i + 1}/{total})")
        dest = _dest_for(product, lang, rel)
        try:
            data = _get(page_path + ".html")
        except Exception as exc:
            print(f"  [DL ERR   ]  {name}: {exc}")
            rows.append({"product": name, "status": "error",
                         "detail": f"download error: {exc}", "path": page_path})
            continue
        if b"<html" not in data[:4096].lower():
            print(f"  [BAD HTML ]  {name}  ({page_path})")
            rows.append({"product": name, "status": "bad_html",
                         "detail": "the page did not render as HTML",
                         "path": page_path})
            continue
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        shown = os.path.relpath(dest, OUT_ROOT)
        print(f"  [OK {len(data)//1024:5}KB]  {shown}")
        rows.append({"product": name, "status": "ok",
                     "file": os.path.basename(dest), "folder": shown,
                     "kb": len(data) // 1024,
                     "detail": f"saved to {shown} ({len(data)//1024} KB)",
                     "path": page_path})
        ok += 1

    _emit(1.0, "cancelled" if cancelled else "done")
    print(f"\nDownloaded {ok}/{total} pages into {OUT_ROOT}"
          + (" (cancelled)" if cancelled else ""))
    return {"ok": ok, "total": total, "out_root": OUT_ROOT, "rows": rows,
            "cancelled": cancelled}


def main():
    res = download_all()
    miss = [r for r in res["rows"] if r["status"] != "ok"]
    if miss:
        print("Not downloaded:")
        for r in miss:
            print(f"  - {r['product']}: {r['detail']}")


if __name__ == "__main__":
    main()
