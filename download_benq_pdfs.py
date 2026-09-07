#!/usr/bin/env python3
"""Download generated BenQ PDFs from the AEM Guides server for a product list.

For each product we locate its ditamap, read the map's jcr:content `pdfPath`
(the generated-output PDF), and download it to:

    benq_pdfs/<PRODUCT_NAME>/<pdf_filename>.pdf

Run:  python download_benq_pdfs.py
"""
import base64
import hashlib
import json
import os
import re
import shutil
import sys
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# The AEM host moves between environments, so it is configurable without editing
# this file: set BENQ_AEM_HOST (and BENQ_AEM_USER / BENQ_AEM_PASSWORD) instead.
HOST = os.environ.get("BENQ_AEM_HOST", "http://139.59.13.139:4502").rstrip("/")
USER = os.environ.get("BENQ_AEM_USER", "admin")
PASSWORD = os.environ.get("BENQ_AEM_PASSWORD", "tG8#vN2^pL5*xW9@")

CONNECT_TIMEOUT = float(os.environ.get("BENQ_AEM_TIMEOUT", "30"))
_RETRIES = 3
PROJECT = "/content/dam/projects/benq-aem-guides"
# Where AEM Guides writes every generated PDF: <OUTPUTS_ROOT>/<lang>/pdfs/*.pdf
OUTPUTS_ROOT = os.environ.get("BENQ_AEM_OUTPUTS_ROOT", "/content/dam/fmdita-outputs")
# Comma-separated language codes to pull, or "*" for every language present.
OUTPUT_LANGS = os.environ.get("BENQ_AEM_OUTPUT_LANGS", "en")
# Only PDFs baked from a map under one of these roots count. A legacy tree at
# /content/dam/benq-aem-guides holds older copies of most of the same products
# under different folder names (`ams` vs `AMS_UM_EN`), so including it would
# download many products twice under two names. Set to "*" to take every root.
SOURCE_ROOTS = os.environ.get("BENQ_AEM_SOURCE_ROOTS", PROJECT)
# Only these category folders hold the products we ship. The rest of the tree
# (`BenQ`, `Shared`, `Corp`, loose one-off folders) is scratch or duplicate
# copies of the same manuals. Shared by the PDF and the HTML crawl; "*" takes
# every category.
GUIDE_CATEGORIES = os.environ.get(
    "BENQ_AEM_GUIDE_CATEGORIES", "zowie,business,consumer,infty,education")
OUT_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "benq_pdfs")

PRODUCTS = [
    "G85T_EN_V9", "GW2291_EN_V0", "GV32_EN_V1.00", "ideaCamS1_UM_EN",
    "RE04A_UM_V1.2_EN", "PCS_EN_V1.04", "TEY41_UM_EN_V1.00", "TEY1C_UM_EN",
    "Stylus_UM_EN_V1.02", "XL-Setting-to-Share_EN_V2.00", "ST04_UM_V2_EN",
    "TEY1C_ RS_4J.FCD01.001 - table", "PDP_RS_ClassA", "DV01K_UM_EN_V1.1",
    "BSH_EN_V1.05", "EW90-EM-V3", "BDH01_EN_V1.02", "EW270Q_EN_V1",
    "RD280UG_Timing_Table", "PD06U-EM-V2", "CF23_F MindDuo Max",
    "i800_i800ST_UM_ZH-CN_V1.02",
]

_AUTH = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()


def wanted_categories():
    """The category folder names to crawl, lowercased; None means every one."""
    if GUIDE_CATEGORIES.strip() == "*":
        return None
    return {c.strip().lower() for c in GUIDE_CATEGORIES.split(",") if c.strip()}


class ServerUnreachable(RuntimeError):
    """The AEM host did not answer at all — nothing here can be downloaded."""


def check_server(timeout: float = 6.0) -> None:
    """Fail fast, and in plain language, when the AEM host is not answering.

    Without this the run made one request per product, each waiting out its own
    timeout, and surfaced the last line of a traceback — "<urlopen error timed
    out>" — which says nothing about which host was tried or why.
    """
    parsed = urllib.parse.urlparse(HOST)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return
    except OSError as exc:
        raise ServerUnreachable(
            f"Cannot reach the AEM server at {host}:{port} ({exc.strerror or exc}). "
            f"The host is not answering, so no PDFs can be downloaded. Check that "
            f"the server is running and reachable from this machine (VPN, firewall, "
            f"or a changed address), then try again. Point the app at a different "
            f"server by setting the BENQ_AEM_HOST environment variable."
        ) from None


def _get(path):
    url = HOST + urllib.parse.quote(path, safe="/:?=&%")
    req = urllib.request.Request(url, headers={"Authorization": _AUTH})
    last = None
    for attempt in range(_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError:
            raise                     # a real answer: 401, 404 — do not retry
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            last = exc                # transient: a slow map or a dropped socket
            if attempt == _RETRIES - 1:
                break
            time.sleep(1.5 * (attempt + 1))
    raise ServerUnreachable(
        f"No response from {HOST} after {_RETRIES} attempts while fetching "
        f"{path} ({last}). The server is unreachable or too slow to answer."
    )


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _strip_version(n):
    # drop trailing version-ish tokens for base matching (v100, v9, …)
    return re.sub(r"(umen|en|v)\d.*$", "", n)


def product_folder(path):
    parts = path.split("/")
    return parts[-3] if parts[-2].lower() == "maps" else parts[-2]


def _match_in(folders, product):
    n = _norm(product)
    if n in folders:
        return folders[n], "exact"
    # prefix either direction (handles V1.0 vs V1.00, _EN suffix, etc.)
    cands = [(k, v) for k, v in folders.items() if k.startswith(n) or n.startswith(k)]
    if cands:
        cands.sort(key=lambda kv: -len(os.path.commonprefix([kv[0], n])))
        return cands[0][1], "fuzzy"
    # base (version stripped)
    b = _strip_version(n)
    cands = [v for k, v in folders.items() if b and _strip_version(k) == b]
    if cands:
        return cands[0], "base"
    # loose: ignore doc-type codes that vary (EM vs EN vs UM, etc.)
    loose = lambda x: re.sub(r"(em|en|um|ug)", "", x)
    cands = [v for k, v in folders.items() if loose(k) == loose(n)]
    if cands:
        return cands[0], "loose"
    return None, "none"


def _clean_folder(name):
    return re.sub(r"[^\w.\- ]", "_", name).strip() or "map"


def discover_ditamaps():
    """Every .ditamap under the whole benq-aem-guides project.

    Returns a list of (parent_folder_name, map_path), one entry per ditamap,
    sorted by folder name. This is a full crawl — no product allow-list — so any
    folder that has a map is included.
    """
    # p.guessTotal=true is required: without it querybuilder stops counting (and
    # returning) hits early, so a deep tree yields only the first couple of maps.
    raw = _get(f"/bin/querybuilder.json?path={PROJECT}&type=dam:Asset"
               f"&nodename=%2a.ditamap&p.limit=-1&p.guessTotal=true"
               f"&p.hits=selective&p.properties=jcr:path")
    hits = json.loads(raw).get("hits", [])
    seen, maps = set(), []
    for h in hits:
        mp = h.get("jcr:path")
        if not mp or not mp.lower().endswith(".ditamap") or mp in seen:
            continue
        seen.add(mp)
        maps.append((product_folder(mp), mp))
    maps.sort(key=lambda t: t[0].lower())
    return maps


_OUT_SUFFIX_RE = re.compile(r"[ _-]*benq[ _-]*pdf$", re.I)
# Language folders look like `en`, `zh-cn` — never a product or map folder name.
_LANG_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2,3})?$")


def _from_source_path(source_path):
    """Product and language from the ditamap a generated PDF was baked from.

    `/content/dam/projects/benq-aem-guides/en/Consumer/Monitor/ma270s/Maps/
    ma270s.ditamap` -> ('ma270s', 'en', 'Consumer'). It is the only reliable
    answer: most generated PDFs sit in one flat `pdfs/` folder with no language
    folder at all, and their file names carry regeneration suffixes ("_benQ V2",
    "_BenQ PDF", "Test pdf") rather than a clean product name.
    """
    parts = [x for x in source_path.strip("/").split("/") if x]
    if not parts:
        return "", "", ""
    lang = next((x for x in parts if _LANG_RE.match(x)), "")
    # The category is the folder directly beneath the language folder.
    category = ""
    if lang:
        i = parts.index(lang)
        if i + 1 < len(parts):
            category = parts[i + 1]
    # The product is the folder holding the map — one level up when the map sits
    # in a `Maps/` subfolder, as it does throughout the BenQ project.
    idx = parts.index("Maps") if "Maps" in parts else len(parts) - 1
    product = parts[idx - 1] if idx >= 1 else os.path.splitext(parts[-1])[0]
    return product, lang, category


def _product_from_output(pdf_path):
    """Fallback for a PDF with no recorded source: read its own path.

    `.../en/pdfs/pd06u_BenQ PDF.pdf` -> ('pd06u', 'en'); a PDF sitting directly
    in the root `pdfs/` folder has no language segment, so it reports none.
    """
    parts = pdf_path.strip("/").split("/")
    stem = os.path.splitext(parts[-1])[0]
    stem = _OUT_SUFFIX_RE.sub("", stem).strip() or parts[-1]
    lang = ""
    if "pdfs" in parts:
        idx = parts.index("pdfs")
        if idx > 0 and _LANG_RE.match(parts[idx - 1]):
            lang = parts[idx - 1]
    return stem, lang


def _modified_at(hit):
    """`jcr:lastModified` as a sortable datetime; missing dates sort oldest."""
    raw = (hit.get("jcr:content") or {}).get("jcr:lastModified") or ""
    try:
        return datetime.strptime(raw, "%a %b %d %Y %H:%M:%S GMT%z")
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def discover_output_pdfs(languages=None):
    """Every generated PDF under OUTPUTS_ROOT, filtered by selected languages.

    Returns a sorted list of (product, lang, pdf_path). A product can have more
    than one generated export; each asset is retained so the local download
    matches the complete inventory in the selected guide categories.
    """
    raw = _get(f"/bin/querybuilder.json?path={OUTPUTS_ROOT}&nodename=%2a.pdf"
               f"&p.limit=-1&p.guessTotal=true&p.hits=selective"
               f"&p.properties=jcr:path%20jcr:content/sourcePath"
               f"%20jcr:content/jcr:lastModified")
    hits = json.loads(raw).get("hits", [])
    selected = OUTPUT_LANGS if languages is None else languages
    wanted = None if selected.strip() == "*" else {
        s.strip().lower() for s in selected.split(",") if s.strip()}
    roots = None if SOURCE_ROOTS.strip() == "*" else [
        r.strip().rstrip("/") for r in SOURCE_ROOTS.split(",") if r.strip()]
    categories = wanted_categories()
    seen, out = set(), []
    for h in hits:
        p = h.get("jcr:path")
        if not p or not p.lower().endswith(".pdf") or p in seen:
            continue
        seen.add(p)
        source = (h.get("jcr:content") or {}).get("sourcePath") or ""
        if roots is not None and not any(
                source == r or source.startswith(r + "/") for r in roots):
            continue
        if source:
            product, lang, category = _from_source_path(source)
        else:
            product, lang = _product_from_output(p)
            category = ""
        if not product:
            continue
        if wanted is not None and lang.lower() not in wanted:
            continue
        if categories is not None and category.lower() not in categories:
            continue
        out.append((product, lang, p))
    out.sort(key=lambda t: (t[0].lower(), t[1]))
    return out


def download_all(progress_cb=None, should_cancel=None, languages=None):
    """Crawl every language folder under OUTPUTS_ROOT and download EVERY
    generated PDF into:

        benq_pdfs/<PRODUCT>/<PRODUCT>.pdf          (for the default `en`)
        benq_pdfs/<PRODUCT>__<lang>/<PRODUCT>__<lang>.pdf   (other languages)

    Each PDF is named after its product (the generated file's stem, minus the
    trailing "_BenQ PDF"). Set BENQ_AEM_OUTPUT_LANGS to a comma list or "*".

    progress_cb(frac, msg): optional callback (frac in 0..1) for the web UI.
    should_cancel(): optional predicate polled between products; when it returns
    true the crawl stops where it is and reports what it already downloaded.
    Returns {ok, total, out_root, cancelled, rows:[{product, status, ...}]}.
    Each row's status is one of: ok | error | bad_pdf.
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
    _emit(0.02, "crawling every folder for generated PDFs…")
    pdfs = discover_output_pdfs(languages=languages)

    # Replace the managed local snapshot only after AEM has responded and its
    # inventory is known, so a failed discovery cannot erase prior downloads.
    if os.path.isdir(OUT_ROOT):
        shutil.rmtree(OUT_ROOT)
    os.makedirs(OUT_ROOT)
    rows, ok = [], 0
    total = len(pdfs)
    cancelled = False
    destinations = {}
    for product, lang, pdf_path in pdfs:
        name = product if (not lang or lang.lower() == "en") else f"{product}__{lang}"
        key = (_clean_folder(name), _clean_folder(os.path.basename(pdf_path)))
        destinations.setdefault(key, []).append(pdf_path)
    for i, (product, lang, pdf_path) in enumerate(pdfs):
        if _cancelled():
            cancelled = True
            print(f"  [CANCELLED]  stopped after {i} of {total}")
            break
        name = product if (not lang or lang.lower() == "en") else f"{product}__{lang}"
        name = _clean_folder(name)
        _emit(0.05 + 0.93 * i / max(total, 1), f"{name} ({i + 1}/{total})")
        dest_dir = os.path.join(OUT_ROOT, name)
        # A product can have multiple exports in AEM. Preserve each generated
        # filename so later assets do not overwrite an earlier one.
        asset_name = _clean_folder(os.path.basename(pdf_path))
        if len(destinations[(name, asset_name)]) > 1:
            stem, ext = os.path.splitext(asset_name)
            asset_name = f"{stem}_{hashlib.sha1(pdf_path.encode()).hexdigest()[:8]}{ext}"
        dest = os.path.join(dest_dir, asset_name)
        try:
            data = _get(pdf_path)
        except Exception as exc:
            print(f"  [DL ERR   ]  {name}: {exc}")
            rows.append({"product": name, "status": "error",
                         "detail": f"download error: {exc}", "path": pdf_path})
            continue
        if data[:4] != b"%PDF":
            print(f"  [BAD PDF  ]  {name}  ({pdf_path})")
            rows.append({"product": name, "status": "bad_pdf",
                         "detail": "downloaded file is not a PDF", "path": pdf_path})
            continue
        os.makedirs(dest_dir, exist_ok=True)
        with open(dest, "wb") as f:
            f.write(data)
        print(f"  [OK {len(data)//1024:5}KB]  {name}/{asset_name}")
        rows.append({"product": name, "status": "ok", "file": asset_name,
                     "folder": name, "kb": len(data) // 1024,
                 "detail": f"saved to {name}/{asset_name} ({len(data)//1024} KB)",
                     "path": pdf_path})
        ok += 1

    _emit(1.0, "cancelled" if cancelled else "done")
    print(f"\nDownloaded {ok}/{total} into {OUT_ROOT}"
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
