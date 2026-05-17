# RegWatch

Regulatory change watcher — scrape agency pages, diff snapshots, and dispatch webhooks when regulations change.

**Agencies tracked:**
- **EPA TSCA** — US Toxic Substances Control Act inventory
- **ECHA REACH** — EU chemicals regulation
- **China MEE** — Ministry of Ecology and Environment

## Quickstart

```bash
# 1. Create a virtual environment & install
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Start PostgreSQL
docker compose up -d

# 3. Copy env file (edit if needed)
cp .env.example .env

# 4. Run the API server
uvicorn app.main:app --reload --port 8000
```

Open [http://localhost:8000/docs](http://localhost:8000/docs) for the interactive Swagger UI.

## Project Structure

```
regwatch/
├── app/
│   ├── main.py              # FastAPI app entry point
│   ├── database.py          # Async SQLAlchemy engine & session
│   ├── models.py            # ORM models (Record)
│   ├── schemas.py           # Pydantic request/response schemas
│   ├── api/
│   │   ├── records.py       # /records endpoints
│   │   └── webhooks.py      # /webhooks endpoints
│   ├── engine/
│   │   ├── diff.py          # Unified diff computation
│   │   └── webhook.py       # Webhook dispatcher (HMAC-signed)
│   └── scrapers/
│       ├── base.py          # Abstract base scraper
│       ├── epa_tsca.py      # EPA TSCA scraper
│       ├── echa_reach.py    # ECHA REACH scraper
│       └── china_mee.py     # China MEE scraper
├── alembic/                 # Database migrations (Alembic)
├── tests/
├── docker-compose.yml       # PostgreSQL 16
├── pyproject.toml
└── README.md
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness check |
| `GET` | `/records` | List records (paginated, filterable by agency) |
| `GET` | `/records/{id}` | Get a single record with full `raw_text` |
| `POST` | `/webhooks/subscribe` | Register a webhook subscriber |
| `GET` | `/webhooks/subscriptions` | List all subscribers |
