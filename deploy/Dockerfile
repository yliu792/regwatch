# ── Production Dockerfile for RegWatch ──────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# System dependencies for lxml / beautifulsoup4
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 libxslt1.1 \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY pyproject.toml .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e ".[dev]" \
    && pip install --no-cache-dir apscheduler

# Copy app code
COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini .

# Run migrations then start
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
