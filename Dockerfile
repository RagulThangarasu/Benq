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
RUN pip install --no-cache-dir -r requirements.txt

# copy app
COPY . /app

# use a non-root user
RUN useradd --no-log-init -m appuser && chown -R appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1
ENV PORT=10000

EXPOSE 10000

CMD ["gunicorn", "-w", "4", "-b", "0.0.0.0:10000", "app:app"]
