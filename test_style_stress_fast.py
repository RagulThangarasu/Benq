#!/usr/bin/env python3
"""Single-pass stress test: run style_validation.main once per product pair.

Catches crashes, empty reports, schema violations — all in one sweep
instead of repeated validate_style calls per test function.

Run:  python test_style_stress_fast.py
"""
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "content_validation"))

import fitz
from style_validation import main, validate_style, CATEGORY_ORDER, SITES_CATEGORY_ORDER

PROD_DIR  = os.path.join(HERE, "PDF", "prod")
STAGE_DIR = os.path.join(HERE, "PDF", "stage")
OUT_DIR   = os.path.join(HERE, "scratch", "stress_reports")
os.makedirs(OUT_DIR, exist_ok=True)

REQUIRED_KEYS = {"category", "severity", "topic", "pages", "expected",
                 "actual", "issue", "fix"}


def _first_pdf(folder):
    for root, _dirs, files in os.walk(folder):
        for f in sorted(files):
            if f.lower().endswith(".pdf"):
                return os.path.join(root, f)
    return None


def discover_pairs():
    prod_folders  = {d for d in os.listdir(PROD_DIR)
                     if os.path.isdir(os.path.join(PROD_DIR, d))}
    stage_folders = {d for d in os.listdir(STAGE_DIR)
                     if os.path.isdir(os.path.join(STAGE_DIR, d))}
    matched = sorted(prod_folders & stage_folders)
    pairs = []
    for folder in matched:
        p = _first_pdf(os.path.join(PROD_DIR, folder))
        s = _first_pdf(os.path.join(STAGE_DIR, folder))
        if p and s:
            pairs.append((folder, p, s))
    return pairs


def run_pair(label, prod, stage, mode="full"):
    """Run main() for one pair, return (status, details) dict."""
    out = os.path.join(OUT_DIR, f"{label.replace(' ', '_')}_{mode}_report.pdf")
    t0 = time.time()
    try:
        findings = main(prod, stage, out)
        elapsed = time.time() - t0

        # Check report file
        if not os.path.exists(out) or os.path.getsize(out) == 0:
            return {"status": "EMPTY_REPORT", "elapsed": elapsed}

        # Check report is valid PDF with >=2 pages
        doc = fitz.open(out)
        page_count = doc.page_count
        front_text = doc[0].get_text()
        doc.close()
        report_ok = page_count >= 2 and "Style Validation Report" in front_text

        # Check findings schema
        schema_errors = []
        for f in findings:
            missing = REQUIRED_KEYS - set(f)
            if missing:
                schema_errors.append(f"missing keys {missing}")
            if f.get("category") not in CATEGORY_ORDER:
                schema_errors.append(f"bad category: {f.get('category')}")
            if f.get("severity") not in ("High", "Medium", "Low", "Info"):
                schema_errors.append(f"bad severity: {f.get('severity')}")

        return {
            "status": "OK" if report_ok and not schema_errors else "ISSUES",
            "elapsed": elapsed,
            "findings": len(findings),
            "report_pages": page_count,
            "report_ok": report_ok,
            "schema_errors": schema_errors[:5],  # cap
        }

    except Exception as exc:
        elapsed = time.time() - t0
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        return {"status": "CRASH", "elapsed": elapsed, "error": tb}


def main_stress():
    pairs = discover_pairs()
    print(f"Discovered {len(pairs)} product pairs\n")

    results = {}

    # ── Phase 1: full mode for every pair ─────────────────────────────────
    print("=" * 70)
    print("PHASE 1: style_validation.main  mode=full")
    print("=" * 70)
    for label, prod, stage in pairs:
        print(f"\n▸ {label}")
        print(f"  PROD:  {os.path.basename(prod)}")
        print(f"  STAGE: {os.path.basename(stage)}")
        r = run_pair(label, prod, stage, mode="full")
        results[(label, "full")] = r
        status = r["status"]
        elapsed = r.get("elapsed", 0)
        if status == "CRASH":
            print(f"  ✗ CRASH in {elapsed:.1f}s")
            print(f"    {r['error'][:300]}")
        elif status == "OK":
            print(f"  ✓ OK in {elapsed:.1f}s — {r['findings']} findings, "
                  f"{r['report_pages']} report pages")
        else:
            print(f"  ? {status} in {elapsed:.1f}s — {r}")

    # ── Phase 2: sites mode for every pair ────────────────────────────────
    print("\n" + "=" * 70)
    print("PHASE 2: validate_style  mode=sites")
    print("=" * 70)
    for label, prod, stage in pairs:
        print(f"\n▸ {label}")
        t0 = time.time()
        try:
            res = validate_style(prod, stage, mode="sites")
            elapsed = time.time() - t0
            findings, doc_stats = res
            print(f"  ✓ OK in {elapsed:.1f}s — {len(findings)} findings")
            results[(label, "sites")] = {"status": "OK", "elapsed": elapsed,
                                          "findings": len(findings)}
        except Exception as exc:
            elapsed = time.time() - t0
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            results[(label, "sites")] = {"status": "CRASH", "elapsed": elapsed, "error": tb}
            print(f"  ✗ CRASH in {elapsed:.1f}s")
            print(f"    {tb[:300]}")

    # ── Phase 3: cross-product pairs (mismatched TOCs) ────────────────────
    print("\n" + "=" * 70)
    print("PHASE 3: cross-product pairs (mismatched TOCs)")
    print("=" * 70)
    for i in range(min(len(pairs) - 1, 3)):
        a_label, a_prod, _ = pairs[i]
        b_label, _, b_stage = pairs[i + 1]
        cross_label = f"{a_label}_X_{b_label}"
        print(f"\n▸ {cross_label}")
        r = run_pair(cross_label, a_prod, b_stage, mode="full")
        results[(cross_label, "cross")] = r
        if r["status"] == "CRASH":
            print(f"  ✗ CRASH in {r['elapsed']:.1f}s")
            print(f"    {r['error'][:300]}")
        else:
            print(f"  ✓ {r['status']} in {r['elapsed']:.1f}s — "
                  f"{r.get('findings', '?')} findings")

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    crashed = [(k, v) for k, v in results.items() if v["status"] == "CRASH"]
    issues  = [(k, v) for k, v in results.items() if v["status"] not in ("OK", "CRASH")]

    if not crashed and not issues:
        print(f"\n  ✓ ALL {len(results)} runs passed — no crashes, no schema issues.\n")
    else:
        if crashed:
            print(f"\n  ✗ {len(crashed)} CRASH(ES):")
            for (label, mode), v in crashed:
                print(f"    - {label} [{mode}]")
                # Print the full traceback for diagnosis
                print(v["error"])
        if issues:
            print(f"\n  ? {len(issues)} issue(s):")
            for (label, mode), v in issues:
                print(f"    - {label} [{mode}]: {v['status']}")

    return 1 if crashed else 0


if __name__ == "__main__":
    sys.exit(main_stress())
