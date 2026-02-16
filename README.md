# Cycle MCP Server

Cycle MCP Server syncs track/routine data from base44 into PostgreSQL, exposes an MCP server for playlist tooling, and includes a FastAPI backend for playlist generation and webhook ingestion.

## What Is In This Repo

- `sync.py`, `sync_routines.py`, `sync_trackfeedback.py`, `sync_all.py`: base44 -> Postgres sync jobs
- `mcp_server.py`: MCP server (stdio, SSE, streamable HTTP)
- `webapp_api.py`: FastAPI service that calls MCP + optional OpenAI curation
- `sql/`: Postgres schema SQL
- `docker-compose.yml`: local stack (`postgres`, `mcp-server`, `webapi`, optional `sync` profile)
- `manage_services.sh`: local process wrapper for MCP + Web API

## Prerequisites

- Python 3.13+ (Dockerfiles currently use Python 3.14 slim)
- PostgreSQL 14+
- base44 API credentials

## Configuration

Copy `.env.example` and fill your values:

```bash
cp .env.example .env
```

Important variables:

- base44: `BASE44_API_KEY`, `BASE44_API_URL`, `BASE44_APP_ID`
- database: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_CONNECT_TIMEOUT`
- MCP: `MCP_TRANSPORT`, `MCP_HOST`, `MCP_PORT`, `MCP_MOUNT_PATH`, `MCP_SSE_PATH`, `MCP_MESSAGE_PATH`, `MCP_HTTP_PATH`, `MCP_LOG_LEVEL`
- optional MCP bearer auth: `MCP_AUTH_BEARER_TOKEN`, `MCP_AUTH_SCOPES`, `MCP_AUTH_ISSUER_URL`, `MCP_AUTH_RESOURCE_URL`, `MCP_AUTH_CLIENT_ID`
- Web API auth/upstream: `WEBAPP_API_KEY`, `MCP_SERVER_URL`, `MCP_SERVER_BEARER_TOKEN`
- webhook security/state: `WEBHOOK_SECRET`, `WEBHOOK_MAX_SKEW_SECONDS`, `WEBHOOK_STATE_FILE`, `WEBHOOK_MAX_EVENT_IDS`
- OpenAI: `OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_TIMEOUT_SECONDS`

## Database Initialization

Run schema files from `sql/`:

```bash
psql -h localhost -U your_db_user -d choreography -f sql/schema.sql
psql -h localhost -U your_db_user -d choreography -f sql/schema_routines.sql
psql -h localhost -U your_db_user -d choreography -f sql/schema_trackfeedback.sql
```

## Sync Data from base44

Run all syncs:

```bash
python sync_all.py
```

Or run individually:

```bash
python sync.py
python sync_routines.py
python sync_trackfeedback.py
```

## Run MCP Server

Install minimum dependencies for sync + MCP:

```bash
pip install -r requirements.txt mcp
```

Run stdio transport:

```bash
python mcp_server.py --transport stdio
```

Run streamable HTTP (default HTTP path `/mcp`):

```bash
python mcp_server.py --transport streamable-http --host 0.0.0.0 --port 8000
```

Run SSE:

```bash
python mcp_server.py --transport sse --host 0.0.0.0 --port 8000
```

## Run Web API

Install Web API dependencies:

```bash
pip install -r requirements.txt fastapi uvicorn httpx mcp pydantic
```

Start API:

```bash
uvicorn webapp_api:app --host 0.0.0.0 --port 8080
```

Health check:

```bash
curl http://127.0.0.1:8080/health
```

## Local Service Wrapper

Use `manage_services.sh` to run MCP + Web API together:

```bash
./manage_services.sh start
./manage_services.sh status
./manage_services.sh logs
./manage_services.sh stop
```

Current defaults from script:

- MCP: `127.0.0.1:8000`
- Web API: `0.0.0.0:8080`

Override example:

```bash
MCP_TRANSPORT=streamable-http MCP_HOST=0.0.0.0 MCP_PORT=8000 WEBAPI_PORT=8081 ./manage_services.sh start
```

## Docker Compose Deployment

Compose services:

- `postgres` (`postgres:18`)
- `mcp-server` (from `Dockerfile.mcp`)
- `webapi` (from `Dockerfile.webapi`)
- `sync` profile service for one-shot `sync_all.py`

Start stack:

```bash
docker compose up -d --build
```

Run one-off sync (profile service):

```bash
docker compose run --rm --profile sync sync
```

Stop stack:

```bash
docker compose down
```

Ports:

- MCP: `http://localhost:${MCP_HOST_PORT:-8000}/mcp`
- Web API: `http://localhost:${WEBAPI_HOST_PORT:-8080}`

Notes on current compose file:

- `webapi` calls MCP internally at `http://mcp-server:8000/mcp`
- Postgres data volume is `postgres_data_v18`
- DB defaults in compose are `DB_NAME=cyclesync`, `DB_USER=glen`
- Dockerfiles currently install from `requirements_mcp.txt` and `requirements_webapi.txt`
- Postgres init mounts are configured as root-level files (`./schema.sql`, `./schema_routines.sql`, `./schema_trackfeedback.sql`, `./migrate_add_audience.sql`)

## Published Container Images

GitHub Actions (`.github/workflows/docker-publish.yml`) builds and publishes both images to GHCR:

- `ghcr.io/<owner>/<repo>-mcp`
- `ghcr.io/<owner>/<repo>-webapi`

The workflow runs on pushes to `main`, `v*.*.*` tags, and a daily schedule.

## MCP Tools

Current tools exported by `mcp_server.py`:

- `search_tracks`
- `suggest_tracks_for_slot`
- `find_similar_tracks`
- `get_track_details`
- `get_top_rated_tracks`
- `get_feedback_summary`
- `build_class_playlist`
- `build_hybrid_playlist`
- `recommend_class_tracks`
- `list_routines`
- `rate_track`

Also exposed:

- resource: `stats://tracks`
- prompt: `build_class`

## Web API Endpoints

Protected endpoints require `X-API-Key: <WEBAPP_API_KEY>`:

- `POST /api/playlist`
- `POST /api/tracks`
- `GET /api/routines/{routine_id}/tracks`

Public health endpoint:

- `GET /health`

Webhook endpoints (HMAC signature required via `X-Webhook-Timestamp` and `X-Webhook-Signature`):

- `POST /api/v1/choreography/updated`
- `POST /api/v1/routine/updated`

## Example Queries

- `examples/example_queries.sql`
- `examples/example_queries_routines.sql`

## Basic Troubleshooting

Database connection issues:

- verify Postgres is reachable from your configured `DB_HOST`/`DB_PORT`
- verify `.env` credentials
- run `psql -h <host> -U <user> -d <db> -c 'select 1'`

MCP auth failures:

- if `MCP_AUTH_BEARER_TOKEN` is set, clients must send `Authorization: Bearer <token>`
- ensure `MCP_SERVER_BEARER_TOKEN` matches when Web API calls MCP

Webhook signature failures:

- verify `WEBHOOK_SECRET`
- verify sender timestamp skew fits `WEBHOOK_MAX_SKEW_SECONDS`
