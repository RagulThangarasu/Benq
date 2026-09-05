FROM python:3.11-slim

# System dependencies.
#
# tesseract-ocr-all matters as much as the Python packages: the comparison reads
# figure artwork optically, and a language pack that is not installed makes
# tesseract exit on that page. An empty read is indistinguishable from "the
# label is not there", so a missing pack turns into a false "label missing" for
# every label on the page. Installing the full set means any localisation of a
# manual validates without special-casing.
#
# The Noto and DejaVu fonts matter for the same reason at the other end: the PDF
# report quotes the evidence verbatim, and a report that prints Cyrillic, Greek,
# Hebrew, Arabic or CJK findings as black boxes is worse than no report.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    tesseract-ocr \
    tesseract-ocr-all \
    fonts-dejavu-core \
    fonts-noto-core \
    fonts-noto-cjk \
    fonts-noto-extra \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# copy requirements first for caching
COPY requirements.txt ./
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# copy app
COPY . /app

# Fail the build rather than ship an image that silently cannot read half the
# world's manuals.
RUN python scripts/check_language_support.py --strict

# use a non-root user
RUN useradd --no-log-init -m appuser && chown -R appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1
ENV PORT=10000
# Concurrent validations. Keep 1 on small instances (each worker ~= one PDF in
# RAM and needs its own CPU); raise to 2-4 on an instance with more CPU + memory.
ENV VALIDATOR_MAX_PARALLEL=1

EXPOSE 10000

# 1 worker (shared in-memory progress) + threads (serve /progress during a job)
# + long timeout (don't kill long validations on slow free-tier CPU).
CMD ["sh", "-lc", "exec gunicorn app:app --workers 1 --threads 8 --timeout 600 --graceful-timeout 30 -b 0.0.0.0:${PORT:-10000}"]
