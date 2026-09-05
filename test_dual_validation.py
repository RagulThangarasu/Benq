"""Tests for the Content + Visual pipeline (content_validation/dual_*.py).

Run:  .venv/bin/python -m pytest test_dual_validation.py -q
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from content_validation import dual_extract as DX      # noqa: E402
from content_validation import dual_validate as DV     # noqa: E402

PAIR = "tmp_uploads/pairs/2018a63fd3384720ad17e009252f396d"
PROD = os.path.join(PAIR, "prod/PV3200U EN V3 User Manual (1).pdf")
STAGE = os.path.join(PAIR, "stage/PV3200U_EN 4.pdf")
HAVE_PDFS = os.path.exists(PROD) and os.path.exists(STAGE)
needs_pdfs = pytest.mark.skipif(not HAVE_PDFS, reason="sample PDFs not present")


# ── pure helpers, no PDFs needed ─────────────────────────────────────────────
def test_norm_words_drops_layout_numbers():
    assert DX.norm_words("5. Place the monitor") == ["place", "the", "monitor"]
    assert DX.norm_words("Item 12 Description") == ["item", "description"]


def test_norm_words_splits_on_punctuation():
    # "USB-C™" becomes separate word runs, so "cord (Supplied" and
    # "cord(Supplied" compare equal — a pure spacing difference must not read
    # as a content change.
    assert DX.norm_words("USB-C\u2122 port") == ["usb", "ctm", "port"]
    assert DX.norm_words("cord (Supplied") == DX.norm_words("cord(Supplied")


def test_norm_key_is_order_preserving():
    assert DX.norm_key("Care and Cleaning") == "care and cleaning"


def test_reads_verbatim_requires_contiguity():
    idx = [{"usb": [0], "peripherals": [1], "headphone": [7]}]
    assert DV._reads_verbatim(idx, ["usb", "peripherals"])
    # scattered diagram labels must NOT count as a phrase
    assert not DV._reads_verbatim(idx, ["usb", "peripherals", "headphone"])


def test_shingles_window():
    assert "a b c" in DV._shingles(["a", "b", "c"], n=3)


def test_image_labels_are_compared_as_independent_lines(tmp_path, monkeypatch):
    import fitz
    from content_validation import validate_toc_content as validator

    pdf_path = tmp_path / "labels.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "10-90%")
    page.insert_text((72, 90), "0-3000m")
    document.save(pdf_path)
    document.close()
    monkeypatch.setattr(validator, "_figure_regions",
                        lambda _page: [fitz.Rect(60, 60, 180, 120)])

    labels = validator._image_labels(str(pdf_path), set())

    assert labels == [(1, "10-90%"), (1, "0-3000m")]


def test_image_label_line_keys_preserve_numeric_ranges(tmp_path):
    import fitz
    from content_validation import validate_toc_content as validator

    pdf_path = tmp_path / "numeric-label.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "10-90%")
    document.save(pdf_path)
    document.close()

    assert validator._canon("10-90%") in validator._image_label_line_keys(
        str(pdf_path), set())


def test_figure_height_difference_is_reported():
    prod = DX.Document(path="prod.pdf", pages=1, figures=[
        DX.FigureInfo(1, (10, 10, 110, 210), "Figure 1", "figure 1"),
    ])
    stage = DX.Document(path="stage.pdf", pages=1, figures=[
        DX.FigureInfo(1, (10, 10, 110, 110), "Figure 1", "figure 1"),
    ])

    findings = DV._figures_lane(prod, stage)

    assert len(findings) == 1
    assert findings[0].kind == "Image dimensions differ"
    assert "height PROD 200.0 pt, STAGE 100.0 pt" in findings[0].detail


# ── extraction ───────────────────────────────────────────────────────────────
@needs_pdfs
def test_extract_builds_both_views():
    d = DX.extract(PROD)
    assert d.pages > 10
    assert d.blocks, "no content blocks extracted"
    assert any(b.kind == "heading" for b in d.blocks)
    assert d.body_size > 5
    assert d.tables, "no tables found"


@needs_pdfs
def test_sections_span_to_next_same_level_heading():
    d = DX.extract(PROD)
    secs = d.sections()
    assert secs
    named = [(h, b) for h, b in secs if h is not None]
    assert named, "no named sections"
    # a section must carry content, not just its own title
    assert any(len(b) > 0 for _h, b in named)


@needs_pdfs
def test_markdown_and_json_round_trip():
    d = DX.extract(PROD, with_layout=False)
    md = d.to_markdown()
    assert md.count("#") > 5
    js = d.to_json()
    assert '"blocks"' in js and '"tables"' in js


def test_markdown_preserves_block_formatting():
    document = DX.Document(path="sample.pdf", pages=1, blocks=[
        DX.Block("heading", "Setup", "setup", 1, level=2, bold=True),
        DX.Block("para", "Important note", "important note", 1, italic=True),
        DX.Block("list", "Connect cable", "connect cable", 1, marker="number"),
        DX.Block("table_head", "Item | Range", "item range", 1,
                 table_id="input", columns=2),
        DX.Block("table_row", "Signal Input | USB-C", "signal input usb c", 1,
                 table_id="input", columns=2),
    ])

    assert document.to_markdown() == (
        "## **Setup**\n\n*Important note*\n\n1. Connect cable\n\n"
        "| Item | Range |\n\n| --- | --- |\n\n| Signal Input | USB-C |"
    )


@needs_pdfs
def test_toc_lines_excluded_from_content():
    d = DX.extract(PROD)
    for b in d.blocks:
        assert not b.text.strip().endswith(tuple(str(n) for n in range(10))) or \
            "...." not in b.text


# ── lanes ────────────────────────────────────────────────────────────────────
@needs_pdfs
def test_self_comparison_is_clean():
    """A document against itself must produce no differences at all."""
    d = DX.extract(PROD)
    assert DV.content_lane(d, d) == []
    visual = DV.visual_lane(d, d)
    comparative = [f for f in visual
                   if f.kind not in ("Private-use glyph", "HTML entity in text",
                                     "Hyperlink broken", "Image label missing")]
    assert comparative == [], f"self-compare produced {comparative[:3]}"


@needs_pdfs
def test_findings_are_stage_scoped():
    findings, _p, _s = DV.validate(PROD, STAGE)
    assert findings, "expected some differences between the two manuals"
    assert {f.doc for f in findings} == {"STAGE"}, "PROD is the reference"


@needs_pdfs
def test_every_finding_carries_locatable_evidence():
    findings, _p, _s = DV.validate(PROD, STAGE)
    for f in findings:
        assert f.page > 0
        assert f.kind and f.topic and f.detail
        assert f.lane in ("content", "visual")


@needs_pdfs
def test_content_findings_read_verbatim_in_prod():
    """No finding may be an artifact of the section stream's ordering."""
    prod = DX.extract(PROD)
    stage = DX.extract(STAGE)
    idx = DV._page_word_index(PROD, prod.nav_pages)
    for f in DV.content_lane(prod, stage):
        words = DX.norm_words(f.evidence)
        assert DV._reads_verbatim(idx, words), \
            f"fragment does not read this way in PROD: {f.evidence!r}"


@needs_pdfs
def test_metrics_are_sane():
    findings, prod, stage = DV.validate(PROD, STAGE)
    m = DV.metrics(findings, prod, stage)
    for k in ("headings_matched", "topics_clean", "text_reproduced"):
        assert 0.0 <= m[k] <= 100.0
    assert m["content_issues"] + m["visual_issues"] == len(findings)


@needs_pdfs
def test_runs_in_reasonable_time():
    t0 = time.time()
    DV.validate(PROD, STAGE)
    assert time.time() - t0 < 90, "validation is too slow"


# ── report ───────────────────────────────────────────────────────────────────
@needs_pdfs
def test_report_is_written(tmp_path):
    from content_validation import dual_report as DR
    findings, prod, stage = DV.validate(PROD, STAGE)
    out = str(tmp_path / "dual.pdf")
    DR.build(PROD, STAGE, findings, DV.metrics(findings, prod, stage), out,
             shots=False)
    assert os.path.getsize(out) > 2000
    import fitz
    d = fitz.open(out)
    text = "\n".join(d[i].get_text() for i in range(d.page_count))
    d.close()
    assert "Content + Visual Validation" in text
    assert "Metrics" in text
