#!/usr/bin/env python3
"""Report - or enforce - what this machine can read and print.

Two things decide whether a validation run handles a language at all:

    * a Tesseract language pack, to read lettering baked into figure artwork;
    * a Unicode font, to print that lettering back in the PDF report.

Neither raises when missing. The run degrades instead: findings on a page whose
script has no pack are reported as "needs a human look" rather than asserted,
and the PDF falls back to Helvetica. That is the right behaviour at runtime and
the wrong behaviour in a build, so `--strict` turns it into a failure.

    python scripts/check_language_support.py            # report
    python scripts/check_language_support.py --strict   # exit 1 if degraded
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The scripts these manuals actually ship in, and the pack that reads each.
REQUIRED = {
    "eng": "English and every Latin model name",
    "deu": "German", "fra": "French", "spa": "Spanish", "ita": "Italian",
    "por": "Portuguese", "nld": "Dutch", "swe": "Swedish", "pol": "Polish",
    "ces": "Czech", "hun": "Hungarian", "ron": "Romanian", "tur": "Turkish",
    "rus": "Russian", "ukr": "Ukrainian", "bul": "Bulgarian",
    "ell": "Greek", "heb": "Hebrew", "ara": "Arabic", "tha": "Thai",
    "chi_sim": "Simplified Chinese", "chi_tra": "Traditional Chinese",
    "jpn": "Japanese", "kor": "Korean",
}

OK, BAD, WARN = "  ok  ", " MISS ", " warn "


def installed_langs() -> set:
    if not shutil.which("tesseract"):
        return set()
    try:
        out = subprocess.run(["tesseract", "--list-langs"],
                             capture_output=True, text=True, timeout=30)
        return {l.strip() for l in (out.stdout or "").splitlines()[1:] if l.strip()}
    except Exception:
        return set()


def main() -> int:
    strict = "--strict" in sys.argv
    problems = []

    print("Tesseract")
    if not shutil.which("tesseract"):
        print(f"  [{BAD}] not installed - no figure artwork can be read at all")
        problems.append("tesseract")
        have = set()
    else:
        have = installed_langs()
        print(f"  [{OK}] found, {len(have)} language pack(s)")

    missing = [c for c in REQUIRED if c not in have]
    if shutil.which("tesseract"):
        print("\nLanguage packs")
        if missing:
            for code in missing:
                print(f"  [{BAD}] {code:8} {REQUIRED[code]}")
            problems.append("language packs")
        else:
            print(f"  [{OK}] all {len(REQUIRED)} required packs present")

    print("\nUnicode font for the PDF report")
    try:
        from content_validation import compare_report
        font = compare_report.FONT
        if font == "Helvetica":
            print(f"  [{BAD}] none found - non-Latin findings would print as "
                  f"black boxes")
            problems.append("unicode font")
        else:
            print(f"  [{OK}] {font}")
    except Exception as exc:
        print(f"  [{WARN}] could not check ({exc})")

    print("\nPython packages")
    for mod, why in (("fitz", "PDF reading"), ("reportlab", "PDF report"),
                     ("cv2", "artwork detection"), ("numpy", "artwork detection"),
                     ("flask", "web app")):
        try:
            __import__(mod)
            print(f"  [{OK}] {mod:10} {why}")
        except Exception:
            print(f"  [{BAD}] {mod:10} {why}")
            problems.append(mod)

    if not problems:
        print("\nAll languages supported. Nothing to install.")
        return 0

    print("\n" + "-" * 68)
    print("Degraded: " + ", ".join(problems))
    print("Runs will still complete - findings on a script with no pack are")
    print("reported as \"needs a human look\" instead of asserted - but install")
    print("the missing pieces for full coverage:\n")
    print("  macOS         bash scripts/setup.sh")
    print("  Debian/Ubuntu sudo bash scripts/setup.sh")
    print("  Docker        already handled by the Dockerfile")
    return 1 if strict else 0


if __name__ == "__main__":
    raise SystemExit(main())
