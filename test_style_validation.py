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

PROD = os.path.join(HERE, "PDF", "prod", "SW272_EN.pdf")
STAGE = os.path.join(HERE, "PDF", "stage", "sw272_en_v5.pdf")

REQUIRED_KEYS = {"category", "severity", "topic", "pages", "expected",
                 "actual", "issue", "fix"}


@pytest.fixture(scope="module")
def findings():
    return validate_style(PROD, STAGE)


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


def test_info_callout_icon_and_text_checked(findings):
    """Info callouts validate coloured icon + coloured text (not just one colour)."""
    info = [f for f in findings if f["category"] == "Info / notice colour"]
    assert info, "info-callout styling not validated"
    assert any("icon" in f["topic"].lower() for f in info), "icon colour not checked"
    assert any("text" in f["topic"].lower() for f in info), "text colour not checked"
    # grey #333333 labels must be reported as not-coloured
    assert any("#333333" in f["actual"] for f in info)


def test_underlined_links_detected(findings):
    u = [f for f in findings if f["category"] == "Underline to remove"]
    assert u, "underlined text/links not detected"
    assert any("link" in f["actual"].lower() or "link" in f["topic"].lower() for f in u)


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
