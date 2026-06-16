#!/usr/bin/env python3
"""Download generated BenQ PDFs from the AEM Guides server for a product list.

For each product we locate its ditamap, read the map's jcr:content `pdfPath`
(the generated-output PDF), and download it to:

    benq_pdfs/<PRODUCT_NAME>/<pdf_filename>.pdf

Run:  python download_benq_pdfs.py
"""
import base64
import json
import os
import re
import sys
import urllib.parse
import urllib.request

HOST = "http://157.245.100.121:4502"
USER, PASSWORD = "admin", "tG8#vN2^pL5*xW9@"
PROJECT = "/content/dam/projects/benq-aem-guides"
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


def _get(path):
    url = HOST + urllib.parse.quote(path, safe="/:?=&%")
    req = urllib.request.Request(url, headers={"Authorization": _AUTH})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


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


def download_all(progress_cb=None):
    """Download the latest generated PDF for every product in PRODUCTS.

    progress_cb(frac, msg): optional callback (frac in 0..1) for the web UI.
    Returns a dict: {ok, total, out_root, rows:[{product, status, ...}]}.
    Each row's status is one of: ok | not_found | no_pdf | error | bad_pdf.
    """
    def _emit(frac, msg=""):
        if progress_cb:
            try:
                progress_cb(max(0.0, min(1.0, float(frac))), msg)
            except Exception:
                pass

    _emit(0.02, "querying ditamaps…")
    raw = _get(f"/bin/querybuilder.json?path={PROJECT}&type=dam:Asset"
               f"&nodename=%2a.ditamap&p.limit=-1&p.hits=selective&p.properties=jcr:path")
    hits = json.loads(raw).get("hits", [])
    folders = {}  # norm(folder) -> (folder_name, map_path)
    for h in hits:
        mp = h["jcr:path"]
        fld = product_folder(mp)
        folders.setdefault(_norm(fld), (fld, mp))

    os.makedirs(OUT_ROOT, exist_ok=True)
    rows, ok = [], 0
    total = len(PRODUCTS)
    for i, product in enumerate(PRODUCTS):
        _emit(0.05 + 0.93 * i / total, f"{product} ({i + 1}/{total})")
        folder_map, kind = _match_in(folders, product)
        if not folder_map:
            print(f"  [NOT FOUND]  {product}")
            rows.append({"product": product, "status": "not_found",
                         "detail": "no matching ditamap"})
            continue
        folder, map_path = folder_map
        try:
            meta = json.loads(_get(map_path + "/jcr:content.json"))
            pdf_path = meta.get("pdfPath")
        except Exception as exc:
            print(f"  [META ERR ]  {product}: {exc}")
            rows.append({"product": product, "status": "error",
                         "detail": f"metadata error: {exc}"})
            continue
        if not pdf_path:
            print(f"  [NO PDF   ]  {product}  (map {folder}: never generated)")
            rows.append({"product": product, "status": "no_pdf",
                         "detail": f"no pdfPath on {folder}"})
            continue
        # Save under the actual PROD/AEM folder name (the matched ditamap folder),
        # so the local folder name matches the PROD PDF's source folder.
        dest_dir = os.path.join(OUT_ROOT, re.sub(r"[^\w.\- ]", "_", folder).strip())
        dest = os.path.join(dest_dir, os.path.basename(pdf_path))
        try:
            data = _get(pdf_path)
        except Exception as exc:
            print(f"  [DL ERR   ]  {product}: {exc}")
            rows.append({"product": product, "status": "error",
                         "detail": f"download error: {exc}"})
            continue
        if not data[:4] == b"%PDF":
            print(f"  [BAD PDF  ]  {product}  ({pdf_path})")
            rows.append({"product": product, "status": "bad_pdf",
                         "detail": "downloaded file is not a PDF"})
            continue
        os.makedirs(dest_dir, exist_ok=True)
        fname = os.path.basename(pdf_path)
        with open(dest, "wb") as f:
            f.write(data)
        note = "" if kind == "exact" else f" ({kind} from '{product}')"
        print(f"  [OK {len(data)//1024:5}KB]  {folder}/{fname}{note}")
        rows.append({"product": product, "status": "ok", "file": fname,
                     "folder": folder, "kb": len(data) // 1024, "match": kind,
                     "detail": f"saved to {folder}/ ({kind})"})
        ok += 1

    _emit(1.0, "done")
    print(f"\nDownloaded {ok}/{total} into {OUT_ROOT}")
    return {"ok": ok, "total": total, "out_root": OUT_ROOT, "rows": rows}


def main():
    res = download_all()
    miss = [r for r in res["rows"] if r["status"] != "ok"]
    if miss:
        print("Not downloaded:")
        for r in miss:
            print(f"  - {r['product']}: {r['detail']}")


if __name__ == "__main__":
    main()
