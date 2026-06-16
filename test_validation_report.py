"""
PDF validation tests (PROD base vs STAGE) + report generation.

  1. test_toc_structure_matches  - every PROD TOC heading exists in STAGE.
  2. test_toc_content_matches     - every PROD section's content exists in STAGE.

Running the suite generates the colourful PDF report:
    reports/pdf_validation_report.pdf

Run:
    python3 -m pytest test_validation_report.py -v
"""

import pytest

import generate_validation_report as G


@pytest.fixture(scope="module")
def result():
    # Builds the PDF report under reports/ and returns the validation data.
    return G.build_report()


def test_pdfs_exist():
    assert G.PROD_PDF.exists(), f"missing PROD pdf: {G.PROD_PDF}"
    assert G.STAGE_PDF.exists(), f"missing STAGE pdf: {G.STAGE_PDF}"


def test_report_generated(result):
    assert result["pdf_path"].exists(), "PDF report was not generated"
    assert result["pdf_path"].stat().st_size > 0


def test_toc_structure_matches(result):
    """1. TOC matching: no PROD heading may be missing from STAGE."""
    missing = result["toc_missing"]
    if missing:
        listing = "\n".join(f"  - {r['heading']} (PROD p{r['prod_page']})"
                            for r in missing)
        pytest.fail(
            f"{len(missing)} PROD TOC heading(s) missing from STAGE "
            f"(see {result['pdf_path'].name}):\n{listing}"
        )


def test_toc_content_matches(result):
    """2. TOC content matching: no section may have content absent from STAGE."""
    failed = result["content_failed"]
    if failed:
        lines = []
        for r in failed:
            lines.append(f"\n[{r['coverage']:.0f}%] {r['heading']} (p{r['prod_page']}):")
            for text in r["spans"]:
                snippet = text if len(text) <= 100 else text[:97] + "..."
                lines.append(f"    - {snippet}")
        pytest.fail(
            f"{len(failed)}/{len(result['cont_rows'])} section(s) have content "
            f"missing from STAGE (see {result['pdf_path'].name}):" + "".join(lines)
        )
