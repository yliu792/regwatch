# RegWatch

Regulatory change watcher — scrape agency websites, diff snapshots, and dispatch
signed webhooks when regulations change.

**Agencies tracked:**
- **EPA TSCA** — US Toxic Substances Control Act inventory
- **ECHA REACH** — EU Registration, Evaluation, Authorisation of Chemicals
- **China MEE** — Ministry of Ecology and Environment

---

## Architecture

```
                          ┌──────────────────────┐
                          │   APScheduler        │
                          │   (cron per source)  │
                          └──────────┬───────────┘
                                     │ timer fires
                                     ▼
┌──────────┐   POST /scrape   ┌──────────────┐   HTTP GET  ┌──────────────┐
│  Client  │ ────────────────→│  FastAPI     │────────────→│  Scrapers    │
│  (CI/CD) │                  │  (uvicorn)   │             │              │
└──────────┘                  │              │◀────────────│  epa_tsca    │
                              │  /records    │  HTML list  │  echa_reach  │
┌──────────┐   POST /webhooks │  /webhooks   │             │  china_mee   │
│  Your    │◀─────────────────│  /scrape     │             └──────────────┘
│  Server  │   JSON payload   │  /scheduler  │
└──────────┘                  │  /health     │      ┌──────────────┐
                              └──────┬───────┘      │  DiffEngine  │
                                     │              │              │
                                     ▼              │  hash_text() │
                              ┌─────────────┐       │  compute_    │
                              │  PostgreSQL │       │    diff()    │
                              │  (asyncpg)  │       └──────┬───────┘
                              └─────────────┘              │
                                     ▲                     │ ChangeEvent
                                     │                     ▼
                              ┌─────────────┐       ┌──────────────┐
                              │  SQLAlchemy │       │  Webhook     │
                              │  2.0 async  │       │  Dispatcher  │
                              └─────────────┘       │              │
                                                    │ HMAC-SHA256  │
                                                    │ POST JSON    │
                                                    └──────────────┘
        ```

### Flow

1. **Scraper** fetches HTML from an agency website and parses it into `ScrapeResult` objects (one per regulatory entry, each with a stable `record_id` like a CASRN).
2. **DiffEngine** compares each incoming `ScrapeResult` against the last persisted `RegulatoryRecord` by content hash (SHA-256). Three outcomes:
   - *New* — INSERT record
   - *Unchanged* — update `last_seen`
   - *Changed* — compute unified diff, create `ChangeEvent`, update record
3. **WebhookDispatcher** loads active `WebhookRegistration` rows, filters by agency, and POSTs each `ChangeEvent` as a versioned JSON payload. Payloads are optionally signed with HMAC-SHA256.
4. **APScheduler** runs the scrape pipeline on a configurable interval per source (default: EPA 24h, ECHA 12h, MEE 6h).

---

## Quickstart

```bash
# 1. Clone & create virtual environment
git clone <repo-url> regwatch && cd regwatch
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Start PostgreSQL
docker compose up -d

# 3. Configure environment
cp .env.example .env
# Edit .env if your database credentials differ

# 4. Run migrations
alembic upgrade head

# 5. Start the server
uvicorn app.main:app --reload --port 8000
```

Open [http://localhost:8000/docs](http://localhost:8000/docs) for the interactive Swagger UI.

---

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://regwatch:regwatch@localhost:5432/regwatch` | PostgreSQL connection string |
| `REGWATCH_SCHEDULER_ENABLED` | `true` | Master switch for APScheduler |
| `REGWATCH_SCHEDULE_EPA_TSCA` | `86400` | EPA scrape interval (seconds) |
| `REGWATCH_SCHEDULE_ECHA_REACH` | `43200` | ECHA scrape interval (seconds) |
| `REGWATCH_SCHEDULE_CHINA_MEE` | `21600` | MEE scrape interval (seconds) |

---

## API Reference

### Health

```bash
GET /health
→ {"status": "ok"}
```

### Scheduler Status

```bash
GET /scheduler
→ {
    "enabled": true,
    "running": true,
    "jobs": [
      {
        "id": "scrape-epa_tsca",
        "name": "Scrape epa_tsca",
        "next_run_time": "2025-06-15T14:00:00+00:00",
        "trigger": "interval[86400.0]"
      }
    ]
  }
```

### Records

```bash
# List records (paginated, filterable)
GET /records/?source=epa_tsca&status=active&limit=20&offset=0
→ [{"id": "…", "source": "epa_tsca", "record_id": "71-43-2", …}, …]

# Get a single record with full raw_content
GET /records/{id}
→ {"id": "…", "raw_content": "Benzene | 71-43-2 | …", …}

# View change history for a record
GET /records/{id}/history
→ [{"id": "…", "previous_hash": "…", "new_hash": "…", "diff_summary": "…", …}, …]
```

### Webhooks

```bash
# Register a webhook
POST /webhooks
{
  "callback_url": "https://your-server.com/hooks/regwatch",
  "agencies": ["epa_tsca", "echa_reach"],
  "secret": "your-hmac-secret"          # optional
}
→ 201 {"id": "…", "callback_url": "…", "is_active": true, …}

# List all webhooks
GET /webhooks
→ [{"id": "…", "callback_url": "…", "agencies": […], …}, …]

# Remove a webhook
DELETE /webhooks/subscriptions/{id}
→ 204 No Content
```

#### Webhook Payload

When a change is detected, each subscriber receives:

```json
{
  "version": "1",
  "event_id": "550e8400-e29b-41d4-a716-446655440000",
  "source": "epa_tsca",
  "record_id": "550e8400-e29b-41d4-a716-446655440001",
  "previous_hash": "331ea6831dff036ae004ba4a2aab24a249d948529a174caec3893d61d8d93d10",
  "new_hash": "6d4e01e75e91760603e0ad65fd86afaf4e154fd1a00a846210de7c4856b3134e",
  "diff_summary": "--- previous\n+++ current\n@@ -1 +1 @@\n-Benzene | Active\n+Benzene | RESTRICTED",
  "detected_at": "2025-06-15T14:00:00.000000+00:00"
}
```

If a `secret` is configured, the request includes an `X-RegWatch-Signature` header
with an HMAC-SHA256 hex digest of the JSON body.

### Scrape

```bash
# Trigger an on-demand scrape
POST /scrape
{"source": "epa_tsca"}
→ {
    "ok": true,
    "report": {
      "source": "epa_tsca",
      "total_incoming": 3,
      "new_count": 0,
      "changed_count": 1,
      "unchanged_count": 2,
      "stale_count": 0,
      "error_count": 0,
      "has_changes": true,
      "change_ids": ["550e8400-…"]
    }
  }
```

---

## Project Structure

```
regwatch/
├── app/
│   ├── main.py                  # FastAPI app, lifespan, scheduler wiring
│   ├── database.py              # Async SQLAlchemy engine & session factory
│   ├── models.py                # ORM: RegulatoryRecord, ChangeEvent, WebhookRegistration
│   ├── schemas.py               # Pydantic request/response models
│   ├── scheduler.py             # APScheduler — per-source interval jobs
│   ├── api/
│   │   ├── records.py           # GET /records, GET /records/{id}, GET /records/{id}/history
│   │   ├── scrape.py            # POST /scrape
│   │   └── webhooks.py          # POST/GET /webhooks, POST/GET /webhooks/subscribe, DELETE
│   ├── engine/
│   │   ├── diff.py              # hash_text(), compute_diff() — unified diff
│   │   ├── diff_engine.py       # DiffEngine: process(ScrapeResult[]) → DiffReport
│   │   ├── webhook.py           # WebhookDispatcher: POST events to subscribers
│   │   └── scrape_pipeline.py   # run_scrape_pipeline(): shared by API + scheduler
│   └── scrapers/
│       ├── base.py              # BaseScraper, ScrapeResult
│       ├── epa_tsca.py          # EPA TSCA Inventory parser
│       ├── echa_reach.py        # ECHA REACH / SVHC parser
│       └── china_mee.py         # China MEE notices parser
├── tests/
│   ├── conftest.py              # Test DB engine fixture
│   ├── test_api.py              # Integration tests (FastAPI TestClient)
│   ├── test_change_events.py    # ChangeEvent correctness assertions
│   ├── test_diff_engine.py      # DiffEngine unit tests
│   └── test_epa_tsca.py         # EPA scraper unit tests
├── alembic/                     # Database migrations
├── demo.py                      # End-to-end demo script
├── docker-compose.yml           # PostgreSQL 16
├── deploy/                      # EC2 deployment configs
├── pyproject.toml
└── README.md
```

---

## Scheduled Scraping

RegWatch ships with APScheduler built in. The scheduler starts automatically when
the server boots (unless `REGWATCH_SCHEDULER_ENABLED=false`).

| Source | Default Interval | Env Variable |
|--------|-----------------|--------------|
| EPA TSCA | 24 hours | `REGWATCH_SCHEDULE_EPA_TSCA` |
| ECHA REACH | 12 hours | `REGWATCH_SCHEDULE_ECHA_REACH` |
| China MEE | 6 hours | `REGWATCH_SCHEDULE_CHINA_MEE` |

Set intervals in seconds. Disable entirely with `REGWATCH_SCHEDULER_ENABLED=false`.

You can still trigger on-demand scrapes at any time via `POST /scrape`.

---

## Deployment

See [`deploy/`](deploy/) for:

- **`docker-compose.prod.yml`** — production Docker Compose (app + PostgreSQL + Nginx)
- **`regwatch.service`** — systemd unit for bare-metal EC2
- **`nginx.conf`** — reverse-proxy configuration

### Quick EC2 Deploy

```bash
# 1. Provision an EC2 instance (Amazon Linux 2023 or Ubuntu 22.04)
#    - Security group: open ports 22 (SSH), 80 (HTTP), 443 (HTTPS)

# 2. SSH in and install prerequisites
ssh ec2-user@<your-ip>
sudo dnf install -y git docker nginx certbot python3-certbot-nginx

# 3. Clone and deploy
git clone <repo-url> /opt/regwatch
cd /opt/regwatch
cp .env.example .env
# Edit .env with production values

# 4. Start with Docker Compose
sudo systemctl start docker
sudo docker compose -f deploy/docker-compose.prod.yml up -d

# 5. Configure Nginx + TLS
sudo cp deploy/nginx.conf /etc/nginx/conf.d/regwatch.conf
sudo certbot --nginx -d your-domain.com
sudo systemctl enable --now nginx
```
