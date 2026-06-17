"""Unit tests for site-vs-PDF style checks (false-positive guardrails)."""

from content_validation.style_validation import _sites_check_alignment, _sites_check_spacing_and_padding


def _rec(title, images=None, first_bad=None):
    return {
        "title": title,
        "data": {
            "images": images or [],
            "firstBad": first_bad,
        },
    }


def test_spacing_padding_does_not_flag_consistent_inset_layout():
    findings = []
    rendered = [
        _rec(
            "Page A",
            images=[
                {"w": 420, "h": 220, "align": "left", "gapLeft": 60, "gapRight": 200, "gapAbove": 36},
                {"w": 390, "h": 210, "align": "left", "gapLeft": 58, "gapRight": 230, "gapAbove": 34},
            ],
        )
    ]

    _sites_check_spacing_and_padding(rendered, findings)

    assert not [f for f in findings if f["category"] in ("Image padding", "Space above image")]


def test_spacing_padding_flags_real_left_padding_outlier():
    findings = []
    rendered = [
        _rec(
            "Page B",
            images=[
                {"alt": "img-1", "w": 420, "h": 220, "align": "left", "gapLeft": 28, "gapRight": 220, "gapAbove": 28},
                {"alt": "img-2", "w": 410, "h": 210, "align": "left", "gapLeft": 25, "gapRight": 230, "gapAbove": 30},
                {"alt": "img-3", "w": 430, "h": 230, "align": "left", "gapLeft": 120, "gapRight": 130, "gapAbove": 32},
            ],
        )
    ]

    _sites_check_spacing_and_padding(rendered, findings)

    assert any(f["category"] == "Image padding" for f in findings)


def test_spacing_flags_inconsistent_gap_only_with_clear_outlier_pattern():
    findings = []
    rendered = [
        _rec(
            "Page C",
            images=[
                {"w": 380, "h": 200, "align": "center", "gapLeft": 100, "gapRight": 100, "gapAbove": 24},
                {"w": 380, "h": 200, "align": "center", "gapLeft": 96, "gapRight": 96, "gapAbove": 26},
                {"w": 380, "h": 200, "align": "center", "gapLeft": 98, "gapRight": 98, "gapAbove": 170},
            ],
        )
    ]

    _sites_check_spacing_and_padding(rendered, findings)

    assert any(
        f["category"] == "Space above image" and f["topic"] == "Inconsistent image spacing"
        for f in findings
    )


def test_alignment_checks_text_and_offset_images():
    findings = []
    rendered = [
        _rec(
            "Page D",
            first_bad={"align": "justify", "text": "Example paragraph text"},
            images=[
                {"alt": "fig-1", "align": "offset", "left": 145},
                {"alt": "fig-2", "align": "left", "left": 0},
            ],
        )
    ]

    _sites_check_alignment(rendered, findings)

    cats = {f["category"] for f in findings}
    assert "Content alignment" in cats
    assert "Image alignment" in cats
