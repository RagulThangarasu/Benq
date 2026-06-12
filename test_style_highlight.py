"""Test: STAGE style differences are highlighted (red) with callout comments.

Runs content_validation/highlight_style_issues.py against the sample PROD/STAGE
PDFs and asserts the annotated STAGE PDF is produced with real PDF annotations:
red Highlight marks on the offending lines plus FreeText callout comment boxes.

Run:  python -m pytest test_style_highlight.py -v
   or: python test_style_highlight.py
"""
import os
import sys

import fitz
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "content_validation"))

from highlight_style_issues import annotate  # noqa: E402

PROD = os.path.join(HERE, "PDF", "prod", "SW272_EN.pdf")
STAGE = os.path.join(HERE, "PDF", "stage", "sw272_en_v5.pdf")


@pytest.fixture(scope="module")
def annotated(tmp_path_factory):
    out = tmp_path_factory.mktemp("style") / "stage_highlighted.pdf"
    summary = annotate(PROD, STAGE, str(out))
    return summary, str(out)


def test_output_pdf_created(annotated):
    _, out = annotated
    assert os.path.exists(out)
    assert os.path.getsize(out) > 0
    assert fitz.open(out).page_count > 0


def test_red_highlights_present(annotated):
    """At least one red Highlight annotation exists on the STAGE PDF."""
    summary, out = annotated
    assert summary["highlights"] > 0

    doc = fitz.open(out)
    red_highlights = 0
    for page in doc:
        for a in page.annots():
            if a.type[1] == "Highlight":
                stroke = (a.colors or {}).get("stroke") or []
                # red = high R, low G/B
                if stroke and stroke[0] > 0.6 and stroke[1] < 0.4 and stroke[2] < 0.4:
                    red_highlights += 1
    doc.close()
    assert red_highlights > 0, "no red Highlight annotations found"


def test_callout_comments_present(annotated):
    """Each comment is a FreeText annotation carrying the issue text + a callout line."""
    summary, out = annotated
    assert summary["boxes"] > 0

    doc = fitz.open(out)
    freetext = 0
    has_issue_text = False
    has_callout = False
    for page in doc:
        for a in page.annots():
            if a.type[1] != "FreeText":
                continue
            freetext += 1
            content = (a.info.get("content") or "")
            if "STYLE ISSUE" in content:
                has_issue_text = True
            # callout line is stored in the /CL entry of the annotation dict
            cl = doc.xref_get_key(a.xref, "CL")
            if cl and cl[0] != "null":
                has_callout = True
    doc.close()
    assert freetext == summary["boxes"]
    assert has_issue_text, "comment boxes missing the issue description"
    assert has_callout, "comment boxes missing the callout (line from highlight)"


def test_missing_section_is_skipped_not_crashed(annotated):
    """A section absent from STAGE has no anchor to mark and is reported, not crashed."""
    summary, _ = annotated
    skipped_titles = [t for t, _why in summary["skipped"]]
    assert "Q&A index" in skipped_titles


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
