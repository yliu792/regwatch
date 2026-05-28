#!/bin/bash
set -e

echo "==> Running Alembic migrations..."
alembic upgrade head

echo "==> Starting RegWatch on :8000"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-level info
