# ── Production Dockerfile for RegWatch ──────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# System dependencies for lxml / beautifulsoup4 + psycopg2
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 libxslt1.1 libpq-dev gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps
COPY pyproject.toml .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir ".[dev]" psycopg2-binary

# Copy app code
COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini .

# Run migrations then start
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
