#!/usr/bin/env python3
"""Stress-test style_validation.main across every available PROD/STAGE pair.

Goal: find any crash that would silently drop a report in a batch run.
Each pair runs in its own test so pytest reports exactly which product
blows up and with what traceback.

Run:  python -m pytest test_style_stress.py -v --tb=long
"""
import os
import sys
import traceback

import fitz
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "content_validation"))

from style_validation import main, validate_style, CATEGORY_ORDER, SITES_CATEGORY_ORDER  # noqa: E402

# ── Discover every paired folder under PDF/prod and PDF/stage ──────────────
PROD_DIR  = os.path.join(HERE, "PDF", "prod")
STAGE_DIR = os.path.join(HERE, "PDF", "stage")


def _first_pdf(folder):
    """Return the first .pdf file inside *folder* (may be nested one level)."""
    for root, _dirs, files in os.walk(folder):
        for f in sorted(files):
            if f.lower().endswith(".pdf"):
                return os.path.join(root, f)
    return None


def _discover_pairs():
    """Yield (label, prod_pdf, stage_pdf) for every matched folder."""
    if not os.path.isdir(PROD_DIR) or not os.path.isdir(STAGE_DIR):
        return
    prod_folders  = {d for d in os.listdir(PROD_DIR)
                     if os.path.isdir(os.path.join(PROD_DIR, d))}
    stage_folders = {d for d in os.listdir(STAGE_DIR)
                     if os.path.isdir(os.path.join(STAGE_DIR, d))}
    matched = sorted(prod_folders & stage_folders)
    for folder in matched:
        p = _first_pdf(os.path.join(PROD_DIR, folder))
        s = _first_pdf(os.path.join(STAGE_DIR, folder))
        if p and s:
            yield folder, p, s


PAIRS = list(_discover_pairs())
IDS   = [p[0] for p in PAIRS]

REQUIRED_KEYS = {"category", "severity", "topic", "pages", "expected",
                 "actual", "issue", "fix"}


# ═══════════════════════════════════════════════════════════════════════════
# 1. validate_style must return without crashing for EVERY pair
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("label,prod,stage", PAIRS, ids=IDS)
def test_validate_style_no_crash(label, prod, stage):
    """validate_style() must not raise for any product pair."""
    result = validate_style(prod, stage)
    # Must return (findings_list, doc_stats) tuple
    assert isinstance(result, tuple), f"expected tuple, got {type(result)}"
    findings, doc_stats = result
    assert isinstance(findings, list)
    assert isinstance(doc_stats, dict)


# ═══════════════════════════════════════════════════════════════════════════
# 2. main must produce a non-empty, valid PDF report for EVERY pair
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("label,prod,stage", PAIRS, ids=IDS)
def test_main_produces_report(label, prod, stage, tmp_path):
    """main() must write a multi-page PDF report without crashing."""
    out = str(tmp_path / f"{label}_style_report.pdf")
    findings = main(prod, stage, out)
    assert os.path.exists(out), f"report file not created for {label}"
    assert os.path.getsize(out) > 0, f"report file is empty for {label}"
    doc = fitz.open(out)
    assert doc.page_count >= 2, f"report has only {doc.page_count} page(s)"
    front = doc[0].get_text()
    assert "Style Validation Report" in front, "title page missing"
    doc.close()


# ═══════════════════════════════════════════════════════════════════════════
# 3. Every finding must conform to the expected schema
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("label,prod,stage", PAIRS, ids=IDS)
def test_findings_schema_all_pairs(label, prod, stage):
    """All finding dicts must carry the required keys and valid values."""
    findings, _ = validate_style(prod, stage)
    for f in findings:
        missing = REQUIRED_KEYS - set(f)
        assert not missing, f"[{label}] finding missing keys {missing}: {f}"
        assert f["category"] in CATEGORY_ORDER, \
            f"[{label}] unknown category: {f['category']}"
        assert f["severity"] in ("High", "Medium", "Low", "Info"), \
            f"[{label}] bad severity: {f['severity']}"


# ═══════════════════════════════════════════════════════════════════════════
# 4. Stress-test mode="sites" — same battery, layout-only checks
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("label,prod,stage", PAIRS, ids=IDS)
def test_validate_style_sites_mode(label, prod, stage):
    """validate_style(mode='sites') must not crash for any product pair."""
    result = validate_style(prod, stage, mode="sites")
    assert isinstance(result, tuple)
    findings, doc_stats = result
    assert isinstance(findings, list)
    # sites mode should only produce layout-relevant categories
    for f in findings:
        assert f["category"] in SITES_CATEGORY_ORDER or f["category"] in CATEGORY_ORDER, \
            f"[{label}] sites-mode produced unexpected category: {f['category']}"


# ═══════════════════════════════════════════════════════════════════════════
# 5. Edge: mismatched page counts shouldn't crash
# ═══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("label,prod,stage", PAIRS, ids=IDS)
def test_mismatched_pages_resilient(label, prod, stage):
    """Even when PROD and STAGE differ wildly in length, no crash occurs."""
    prod_doc = fitz.open(prod)
    stage_doc = fitz.open(stage)
    prod_pages = prod_doc.page_count
    stage_pages = stage_doc.page_count
    prod_doc.close()
    stage_doc.close()
    print(f"  {label}: PROD={prod_pages}pp  STAGE={stage_pages}pp  "
          f"(ratio {stage_pages/max(prod_pages,1):.2f})")
    # Already tested above — this test just documents the page-count mismatch
    # and confirms the ratio isn't causing any division-by-zero, etc.
    findings, doc_stats = validate_style(prod, stage)
    assert doc_stats["prod_pages"] == prod_pages
    assert doc_stats["stage_pages"] == stage_pages


# ═══════════════════════════════════════════════════════════════════════════
# 6. Cross-pair: use a prod from one product with a stage from another
#    (totally mismatched TOCs — should NOT crash, just produce many findings)
# ═══════════════════════════════════════════════════════════════════════════
def _cross_pairs():
    """Generate a few cross-product pairings to shake out assumptions."""
    if len(PAIRS) < 2:
        return
    # pair[0] prod × pair[1] stage, and vice-versa
    for i in range(min(len(PAIRS) - 1, 3)):
        a_label, a_prod, _ = PAIRS[i]
        b_label, _, b_stage = PAIRS[i + 1]
        yield f"{a_label}_x_{b_label}", a_prod, b_stage


CROSS = list(_cross_pairs())
CROSS_IDS = [c[0] for c in CROSS]


@pytest.mark.parametrize("label,prod,stage", CROSS, ids=CROSS_IDS)
def test_cross_product_no_crash(label, prod, stage, tmp_path):
    """Feeding mismatched products must not crash main()."""
    out = str(tmp_path / f"{label}_cross_report.pdf")
    findings = main(prod, stage, out)
    assert os.path.exists(out), f"cross-pair report not created for {label}"
    assert os.path.getsize(out) > 0


# ═══════════════════════════════════════════════════════════════════════════
# 7. Batch runner: simulates processing all pairs sequentially
#    (catches state leaks between runs — global caches, file handles, etc.)
# ═══════════════════════════════════════════════════════════════════════════
def test_batch_sequential_no_state_leak(tmp_path):
    """Run main() for every pair in sequence in the same process.

    If any global / module state leaks between runs, the later products
    will crash or produce incorrect results.
    """
    results = {}
    for label, prod, stage in PAIRS:
        out = str(tmp_path / f"{label}_batch.pdf")
        try:
            findings = main(prod, stage, out)
            ok = os.path.exists(out) and os.path.getsize(out) > 0
            results[label] = {"status": "OK" if ok else "EMPTY", "findings": len(findings)}
        except Exception as exc:
            results[label] = {"status": "CRASH", "error": "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__))}

    # Print summary
    print("\n\n=== BATCH STRESS-TEST SUMMARY ===")
    for label, info in results.items():
        status = info["status"]
        if status == "CRASH":
            print(f"  ✗ {label:30}  CRASH")
            print(f"    {info['error'][:200]}")
        else:
            print(f"  {'✓' if status == 'OK' else '?'} {label:30}  {status}  "
                  f"findings={info.get('findings', '?')}")

    crashed = [l for l, i in results.items() if i["status"] == "CRASH"]
    assert not crashed, f"Batch crashes: {crashed}"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "--tb=long", "-x"]))
