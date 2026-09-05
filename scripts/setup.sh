#!/usr/bin/env bash
# Install everything a full-coverage validation run needs.
#
# Python packages alone are not enough. The comparison reads figure artwork
# optically, so it needs Tesseract and a language pack per script; the PDF
# report quotes that artwork back, so it needs a font that can print the script.
# Without either the run still completes - it just stops being able to tell
# "this label was dropped" from "I could not read this label", and says so.
#
#   bash scripts/setup.sh          # macOS (Homebrew)
#   sudo bash scripts/setup.sh     # Debian / Ubuntu
#
# Idempotent: safe to re-run.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

case "$(uname -s)" in
  Darwin)
    if ! command -v brew >/dev/null 2>&1; then
      echo "Homebrew is required: https://brew.sh" >&2
      exit 1
    fi
    say "Installing Tesseract and every language pack (macOS)"
    brew list tesseract      >/dev/null 2>&1 || brew install tesseract
    # ~1.2 GB. This is the package that makes non-English manuals validate.
    brew list tesseract-lang >/dev/null 2>&1 || brew install tesseract-lang
    # macOS ships Arial Unicode, which covers every script these manuals use.
    ;;
  Linux)
    SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
    say "Installing Tesseract, every language pack and Unicode fonts (Debian/Ubuntu)"
    $SUDO apt-get update
    $SUDO apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-all \
      fonts-dejavu-core fonts-noto-core fonts-noto-cjk fonts-noto-extra \
      libgl1 libglib2.0-0
    ;;
  *)
    echo "Unsupported platform $(uname -s). Install tesseract, its language" >&2
    echo "packs and a Unicode font (Noto or DejaVu) by hand, then re-run" >&2
    echo "scripts/check_language_support.py." >&2
    ;;
esac

say "Installing Python packages"
PY="${PYTHON:-python3}"
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  "$PY" -m venv .venv && PY=".venv/bin/python"
fi
"$PY" -m pip install --upgrade pip >/dev/null
"$PY" -m pip install -r requirements.txt

say "Checking coverage"
"$PY" scripts/check_language_support.py
