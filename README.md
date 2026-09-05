# Benq PDF Validation

A Flask-based validation app for BenQ product manuals. It compares PROD and
STAGE PDFs **and** validates a published AEM site against its source PDF.

## Features
- Upload and append PROD/STAGE PDF folders or single documents
- Validate content, style, or both
- Queue management with search and selection
- Generate downloadable validation reports without persisting them server-side
- **Sites Validation** (`/sites-validation`) — validate a live AEM site against a PROD PDF:
  - **Content tab** — crawls every page under an AEM author URL and checks that each
    PDF TOC section's text is present on the site (heading-only sections are validated
    by heading presence, not skipped).
  - **Style tab** — renders each AEM page in a headless browser (Playwright) and flags
    image/layout issues the CMS does not control: typography spec (H1/H2/H3 + body font
    sizes), **line height** (all headings & bullets), image dimensions vs the PDF figure,
    oversized images, images cut off / overflowing, table breaking, and text/image
    **alignment**. (Typography/colour governed by AEM are checked via the rendered styles.)
- Docker-ready for cloud deployment

## Quick start

```bash
bash scripts/setup.sh          # macOS (Homebrew)
sudo bash scripts/setup.sh     # Debian / Ubuntu
```

One command. It installs the Python packages **and** the two system pieces that
`pip` cannot provide — Tesseract with every language pack, and a Unicode font —
then prints a coverage report. It is idempotent, so re-run it any time.

Docker users need nothing: the image installs all of it and the build fails if
anything is missing.

```bash
docker build -t benq-validator .
docker run -p 10000:10000 benq-validator
```

To check an existing machine at any time:

```bash
python scripts/check_language_support.py            # report
python scripts/check_language_support.py --strict   # exit 1 if degraded (CI)
```

## Language support

Manuals ship in every market, so the validator is script-agnostic by design:

- **Comparison** tokenises any script — Latin, Cyrillic, Greek, Hebrew, Arabic,
  Thai, Chinese, Japanese, Korean. Chinese, Japanese, Korean and Thai are
  segmented per character, because they are written without spaces and a whole
  line would otherwise arrive as a single token.
- **Artwork OCR** reads figure lettering in the languages the document is
  actually written in, chosen from the Unicode blocks present on each page, and
  requests only packs that are installed.
- **Reports** register a Unicode font (Arial Unicode, Noto CJK or DejaVu), so
  findings print in their own script instead of as black boxes. Override with
  `REPORT_FONT_PATH=/path/to/font.ttf`.

**Nothing errors when a pack is missing.** A run completes either way; findings
on a page whose script has no pack are reported as *"needs a human look"* with
both crops shown, rather than asserted as defects. The app logs the gap at
startup and each report states it.

## Requirements
- Python 3.11+
- `pip`
- Tesseract + language packs and a Unicode font — installed by `scripts/setup.sh`
  or by the Dockerfile
- PDFs for PROD/STAGE validation
- For the **Sites Validation → Style** tab: Playwright + a Chromium build, and an
  authenticated AEM session (sign in from the Content tab header).

## Install

```bash
cd /Users/ragul/Desktop/Benq
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Sites Validation (Style tab) — headless browser for rendering AEM pages
pip install playwright
python -m playwright install chromium
```

> **Tesseract OCR:** language `*.traineddata` files (the `tessdata/` folder) are not
> committed — they are large blobs. Install them separately if OCR-assisted extraction
> is needed.

## Run locally

```bash
cd /Users/ragul/Desktop/Benq
source .venv/bin/activate
python app.py
```

Then open http://127.0.0.1:5000 in your browser.

## Docker

Build the image:

```bash
docker build -t benq-validator:latest .
```

Run locally:

```bash
docker run --rm -p 10000:10000 benq-validator:latest
```

## Render deployment

This repository includes a `render.yaml` and `Dockerfile` for Render deployment.

1. Create or connect the GitHub repository.
2. Add the project in Render as a Docker web service.
3. Render will build the image from `Dockerfile` and expose the app on the assigned port.

### Render environment
- Ensure the service port is set to `10000` or use the `PORT` environment variable provided by Render.

## GitHub push

If the remote repo exists, push using:

```bash
cd /Users/ragul/Desktop/Benq
git push -u origin main
```

## Project files

- `app.py` — Flask backend and upload/validation endpoints
- `run_validator.py` — wrapper for validator subprocess execution
- `content_validation/validate_toc_content.py` — content validation logic
- `content_validation/style_validation.py` — PDF-vs-PDF style validation logic
- `content_validation/sites_image_validation.py` — renders AEM pages (Playwright) and
  validates image/layout/typography against the PROD PDF
- `templates/index.html` — main frontend UI
- `templates/sites-validation.html` — Sites Validation UI (Content + Style tabs)
- `Dockerfile` — production container build
- `render.yaml` — Render service config

## Notes
- Reports are generated temporarily and returned directly to the user.
- The app uses local temp file generation and streaming for privacy.
