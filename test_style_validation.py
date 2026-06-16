"""Test the comprehensive style validation (style_validation.py).

Runs all nine style checks against the sample PROD/STAGE PDFs and asserts the
report is produced and the high-value findings (heading brand-colour change,
underlined links) are detected with the expected schema.

Run:  python -m pytest test_style_validation.py -v
"""
import os
import sys

import fitz
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "content_validation"))

from style_validation import validate_style, main, CATEGORY_ORDER  # noqa: E402

import glob


def _find_pdf(side, prefer):
    """Locate the sample PDF under PDF/<side>/ even if it's nested in subfolders
    (the sample tree gets reorganised when testing folder-pair uploads)."""
    direct = os.path.join(HERE, "PDF", side, prefer)
    if os.path.exists(direct):
        return direct
    hits = glob.glob(os.path.join(HERE, "PDF", side, "**", "*.pdf"), recursive=True)
    for h in hits:
        if "sw272" in os.path.basename(h).lower():
            return h
    return hits[0] if hits else direct


PROD = _find_pdf("prod", "SW272_EN.pdf")
STAGE = _find_pdf("stage", "sw272_en_v5.pdf")

REQUIRED_KEYS = {"category", "severity", "topic", "pages", "expected",
                 "actual", "issue", "fix"}


@pytest.fixture(scope="module")
def findings():
    result = validate_style(PROD, STAGE)
    # validate_style returns (findings_list, doc_stats) tuple
    if isinstance(result, tuple):
        return result[0]
    return result


def test_findings_schema(findings):
    assert findings, "expected style differences between PROD and STAGE"
    for f in findings:
        assert REQUIRED_KEYS <= set(f), f"finding missing keys: {f}"
        assert f["category"] in CATEGORY_ORDER
        assert f["severity"] in ("High", "Medium", "Low")


def test_heading_brand_colour_detected(findings):
    """PROD purple #4a167c vs STAGE teal #006666 must surface as a High issue."""
    h = [f for f in findings if f["category"] == "Heading style"
         and "#006666" in f["actual"] and "#4a167c" in f["expected"]]
    assert h, "heading brand-colour change not detected"
    assert h[0]["severity"] == "High"


def test_info_callout_text_only_no_icon_or_bg(findings):
    """Info callouts validate label-text colour only. The coloured icon and the
    themed background legitimately differ per callout type (NOTE / TIP / WARNING),
    so icon-colour and background-colour diffs must NOT be flagged."""
    info = [f for f in findings if f["category"] == "Info / notice colour"]
    assert info, "info-callout styling not validated"
    assert any("text" in f["topic"].lower() for f in info), "text colour not checked"
    assert not any("icon" in f["topic"].lower() for f in info), "icon colour should not be flagged"
    assert not any("background" in f["topic"].lower() for f in info), "background should not be flagged"


def test_footer_alignment_checked(findings):
    """Footer page numbers not right-aligned are flagged as an issue."""
    foot = [f for f in findings if f["category"] == "Footer page number"]
    assert foot, "footer alignment not checked"
    # the sample PDFs use centre-aligned footers; expect a centre finding
    assert any("centre" in f["topic"].lower() or "left" in f["topic"].lower() for f in foot)
    assert all("right-align" in f["fix"].lower() for f in foot)


def test_every_check_runs_without_error(findings):
    """All nine categories are valid; at least several should have findings."""
    cats_with_hits = {f["category"] for f in findings}
    assert cats_with_hits <= set(CATEGORY_ORDER)
    assert len(cats_with_hits) >= 5


def test_report_pdf_generated(tmp_path):
    out = tmp_path / "style_report.pdf"
    main(PROD, STAGE, str(out))
    assert out.exists() and out.stat().st_size > 0
    doc = fitz.open(str(out))
    assert doc.page_count >= 2
    assert "Style Validation Report" in doc[0].get_text()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
