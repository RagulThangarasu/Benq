# Benq PDF Validation

A Flask-based PDF validation app for comparing PROD and STAGE PDF folders or single PDF files.

## Features
- Upload and append PROD/STAGE PDF folders or single documents
- Validate content, style, or both
- Queue management with search and selection
- Generate downloadable validation reports without persisting them server-side
- Docker-ready for cloud deployment

## Requirements
- Python 3.11+
- `pip`
- PDFs for PROD/STAGE validation

## Install

```bash
cd /Users/ragul/Desktop/Benq
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

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
- `content_validation/style_validation.py` — style validation logic
- `templates/index.html` — frontend UI
- `Dockerfile` — production container build
- `render.yaml` — Render service config

## Notes
- Reports are generated temporarily and returned directly to the user.
- The app uses local temp file generation and streaming for privacy.
