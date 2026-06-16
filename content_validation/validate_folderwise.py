#!/usr/bin/env python3
"""Folder-wise PROD-vs-STAGE content validation.

Baseline (PROD) : the production PDFs under
    "Final Cleanup files for Pavan to add Structure and fix the alt attribute value/prod_pdfs"
Stage  (STAGE)  : the downloaded PDFs under  Benq/benq_pdfs

For every product folder present in BOTH locations, run the existing
content validation (validate_toc_content.validate) and write one report PDF
per folder. All reports are kept inside this content_validation directory
(folderwise_reports/). A summary index PDF + JSON is written alongside.
"""
import os
import sys
import json
import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent          # .../Benq/content_validation
ROOT = HERE.parent                               # .../Benq
sys.path.insert(0, str(HERE))

from validate_toc_content import validate        # noqa: E402

PROD_BASE = Path(
    "/Users/ragul/Desktop/Final Cleanup files for Pavan to add Structure "
    "and fix the alt attribute value/prod_pdfs"
)
STAGE_BASE = ROOT / "benq_pdfs"
OUT_DIR = HERE / "folderwise_reports"


def _pdfs(folder: Path):
    return sorted(p for p in folder.glob("*.pdf") if not p.name.startswith("._"))


def _first_pdf(folder: Path):
    pdfs = _pdfs(folder)
    return pdfs[0] if pdfs else None


def _score(summary):
    """Rank a validation result so we can auto-pick the best PROD candidate
    when a folder ships several language variants (e.g. CF23 SC + TC)."""
    tot = summary["content_pass"] + summary["content_fail"]
    cov = summary["content_pass"] / tot if tot else 0.0
    return (cov, -summary["toc_missing"])


def main():
    OUT_DIR.mkdir(exist_ok=True)
    prod_folders = {p.name for p in PROD_BASE.iterdir() if p.is_dir()}
    stage_folders = {p.name for p in STAGE_BASE.iterdir() if p.is_dir()}
    common = sorted(prod_folders & stage_folders)
    only_prod = sorted(prod_folders - stage_folders)
    only_stage = sorted(stage_folders - prod_folders)
    all_folders = sorted(prod_folders | stage_folders)

    print(f"Total: {len(all_folders)} | Common: {len(common)} | only-PROD: {len(only_prod)} | "
          f"only-STAGE: {len(only_stage)}")

    rows = []
    for i, name in enumerate(all_folders, 1):
        prod_candidates = _pdfs(PROD_BASE / name)
        stage_pdf = _first_pdf(STAGE_BASE / name)
        print(f"\n[{i}/{len(all_folders)}] === {name} ===")
        if not prod_candidates or not stage_pdf:
            print(f"  SKIP - missing PDF (prod={bool(prod_candidates)}, stage={bool(stage_pdf)})")
            rows.append({
                "folder": name,
                "status": "SKIP",
                "note": "missing PDF on one side",
                "prod_pdf": prod_candidates[0].name if prod_candidates else "",
                "stage_pdf": stage_pdf.name if stage_pdf else ""
            })
            continue

        safe = name.replace("/", "_").replace(" ", "_")
        out_pdf = OUT_DIR / f"{safe}.pdf"

        # When a folder has multiple PROD PDFs (language variants), validate each
        # against the stage PDF and keep the best-scoring one (its report wins).
        best = None
        for prod_pdf in prod_candidates:
            tag = f" [{prod_pdf.name}]" if len(prod_candidates) > 1 else ""
            print(f"  validating{tag} ...")
            try:
                summary = validate(str(prod_pdf), str(stage_pdf), str(out_pdf))
            except Exception as e:  # keep going on the rest
                print(f"  ERROR: {e}")
                continue
            summary["prod_pdf"] = prod_pdf.name
            if best is None or _score(summary) > _score(best):
                best = summary

        # out_pdf currently holds the last-validated candidate; if the winner
        # was an earlier one, re-render its report so the saved PDF matches.
        if best is not None and best["prod_pdf"] != prod_candidates[-1].name:
            validate(str(PROD_BASE / name / best["prod_pdf"]),
                     str(stage_pdf), str(out_pdf))

        if best is None:
            rows.append({"folder": name, "status": "ERROR",
                         "note": "all PROD candidates failed to validate"})
            continue

        verdict = "PASS" if best["content_fail"] == 0 and best["toc_missing"] == 0 else "FAIL"
        best.update({"folder": name, "status": verdict, "stage_pdf": stage_pdf.name})
        rows.append(best)
        print(f"  -> {verdict}  (prod={best['prod_pdf']} | content Pass={best['content_pass']} "
              f"Fail={best['content_fail']} | TOC missing={best['toc_missing']})")

    # ── write JSON + text index ──
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    (OUT_DIR / "summary.json").write_text(json.dumps(
        {"generated": stamp, "only_prod": only_prod, "only_stage": only_stage,
         "results": rows}, indent=2))

    _write_index_pdf(rows, only_prod, only_stage, stamp)

    n_pass = sum(1 for r in rows if r.get("status") == "PASS")
    n_fail = sum(1 for r in rows if r.get("status") == "FAIL")
    print(f"\n==== DONE: {n_pass} PASS / {n_fail} FAIL / "
          f"{len(rows) - n_pass - n_fail} other ====")
    print(f"Reports + index in: {OUT_DIR}")


def _write_index_pdf(rows, only_prod, only_stage, stamp):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle,
                                    Paragraph, Spacer)

    styles = getSampleStyleSheet()
    out = OUT_DIR / "00_INDEX_folderwise_summary.pdf"
    doc = SimpleDocTemplate(str(out), pagesize=A4,
                            title="Folder-wise Content Validation Summary")
    story = [Paragraph("Folder-wise Content Validation", styles["Title"]),
             Paragraph("Baseline = PROD (prod_pdfs) &nbsp;|&nbsp; Stage = STAGE (benq_pdfs)",
                       styles["Normal"]),
             Paragraph(f"Generated: {stamp}", styles["Normal"]),
             Spacer(1, 12)]

    data = [["#", "Product folder", "Verdict", "Content Pass",
             "Content Fail", "TOC missing", "TOC extra"]]
    for i, r in enumerate(rows, 1):
        data.append([str(i), r["folder"], r.get("status", "?"),
                     str(r.get("content_pass", "-")), str(r.get("content_fail", "-")),
                     str(r.get("toc_missing", "-")), str(r.get("toc_extra", "-"))])

    tbl = Table(data, repeatRows=1, colWidths=[20, 200, 50, 60, 55, 60, 55])
    ts = TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#34495e")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f4f6")]),
    ])
    for ri, r in enumerate(rows, 1):
        st = r.get("status")
        col = (colors.HexColor("#1e8449") if st == "PASS"
               else colors.HexColor("#c0392b") if st == "FAIL"
               else colors.HexColor("#b9770e"))
        ts.add("TEXTCOLOR", (2, ri), (2, ri), col)
        ts.add("FONTNAME", (2, ri), (2, ri), "Helvetica-Bold")
    tbl.setStyle(ts)
    story.append(tbl)

    if only_prod or only_stage:
        story.append(Spacer(1, 14))
        story.append(Paragraph("Folders not validated (no matching pair):",
                               styles["Heading3"]))
        if only_prod:
            story.append(Paragraph("Only in PROD: " + ", ".join(only_prod),
                                   styles["Normal"]))
        if only_stage:
            story.append(Paragraph("Only in STAGE: " + ", ".join(only_stage),
                                   styles["Normal"]))

    doc.build(story)
    print(f"Index PDF: {out}")


if __name__ == "__main__":
    main()
