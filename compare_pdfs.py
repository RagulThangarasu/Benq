#!/usr/bin/env python3
"""Compare two PDFs and write both reports.

    python compare_pdfs.py PROD.pdf STAGE.pdf [-o out_dir] [-n name]
"""
from __future__ import annotations

import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from content_validation import pdf_compare as C
from content_validation import compare_report as R


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("prod")
    ap.add_argument("stage")
    ap.add_argument("-o", "--out", default="reports")
    ap.add_argument("-n", "--name", default="")
    args = ap.parse_args()

    stem = args.name or os.path.splitext(os.path.basename(args.prod))[0]

    def progress(pct, msg):
        print("  %3d%%  %s" % (pct, msg), flush=True)

    print("Comparing %s -> %s" % (args.prod, args.stage))
    diffs, prod, stage, pmap = C.compare(args.prod, args.stage, progress)

    meta = {
        "name": stem,
        "title": "%s Content Difference Report" % stem,
        "matched": sum(1 for v in pmap.values() if v),
        "run": datetime.date.today().isoformat(),
    }
    print("  90%%  Rendering evidence and writing both reports", flush=True)
    out = R.build(diffs, prod, stage, args.out, stem, meta)

    c = out["counts"]
    print("\n%d differences  (high %d · medium %d · low %d)"
          % (c["total"], c["high"], c["medium"], c["low"]))
    print("  %d found in artwork, %d found in text" % (c["image"], c["text"]))
    print("\nHTML  %s" % out["html"])
    print("PDF   %s" % out["pdf"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
