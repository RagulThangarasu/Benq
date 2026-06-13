FROM python:3.11-slim

# system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# copy requirements first for caching
COPY requirements.txt ./
RUN pip install --no-cache-dir --root-user-action=ignore -r requirements.txt

# copy app
COPY . /app

# use a non-root user
RUN useradd --no-log-init -m appuser && chown -R appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1
ENV PORT=10000

EXPOSE 10000

# 1 worker (shared in-memory progress) + threads (serve /progress during a job)
# + long timeout (don't kill long validations on slow free-tier CPU).
CMD ["sh", "-lc", "exec gunicorn app:app --workers 1 --threads 8 --timeout 600 --graceful-timeout 30 -b 0.0.0.0:${PORT:-10000}"]
