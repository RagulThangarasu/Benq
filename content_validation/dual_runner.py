"""Entry point used by the app for the Content + Visual mode.

The deliverable is the STAGE PDF itself, marked up: every issue boxed in red on
the page it belongs to, carrying a comment, with a clickable index prepended.
That is more useful than a separate document describing the issues, because the
finding and the page it is about are the same artefact.
"""
from __future__ import annotations

import os

from . import dual_validate as DV
from . import dual_annotate as DA
from . import dual_report as DR

_PROGRESS = None
ANNOTATED_REPORT = True     # False → the standalone tabular report instead


def set_progress_callback(cb):
    global _PROGRESS
    _PROGRESS = cb


def main(prod_path: str, stage_path: str, out_path: str, markdown_dir: str | None = None):
    findings, prod, stage = DV.validate(prod_path, stage_path, _PROGRESS)
    met = DV.metrics(findings, prod, stage)

    if markdown_dir:
        os.makedirs(markdown_dir, exist_ok=True)
        with open(os.path.join(markdown_dir, "PROD.md"), "w", encoding="utf-8") as handle:
            handle.write(prod.to_markdown())
        with open(os.path.join(markdown_dir, "STAGE.md"), "w", encoding="utf-8") as handle:
            handle.write(stage.to_markdown())

    if ANNOTATED_REPORT:
        info = DA.annotate(stage_path, findings, out_path,
                           os.path.basename(prod_path),
                           os.path.basename(stage_path),
                           nav_pages=stage.nav_pages)
        print(f"  content issues: {met['content_issues']} | "
              f"visual issues: {met['visual_issues']}")
        print(f"  marked in STAGE: {info['marked']} "
              f"(index pages: {info['index_pages']})")
    else:
        DR.build(prod_path, stage_path, findings, met, out_path)
        print(f"  content issues: {met['content_issues']} | "
              f"visual issues: {met['visual_issues']}")

    print(f"Report saved: {out_path}")
    return findings, met
