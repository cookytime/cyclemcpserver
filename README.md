# Cycle MCP Server

Sync your music tracks and workout routines from base44 to a local PostgreSQL database for choreography refinement using MCP servers.

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure Environment

Copy the example environment file and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env` with your actual values:
- `BASE44_API_KEY`: Your base44 API key
- `BASE44_API_URL`: base44 API endpoint (update if different)
- `DB_*`: Your PostgreSQL connection details

### 3. Initialize Database

Run the schemas to create the necessary tables:

```bash
# Create tracks tables
psql -h localhost -U your_db_user -d choreography -f schema.sql

# Create routines tables
psql -h localhost -U your_db_user -d choreography -f schema_routines.sql

# Create track feedback tables
psql -h localhost -U your_db_user -d choreography -f schema_trackfeedback.sql

# Apply feedback audience migration (recommended for MCP rating workflow)
psql -h localhost -U your_db_user -d choreography -f migrate_add_audience.sql
```

Or connect to your database and run the schemas manually:

```bash
psql -h localhost -U your_db_user -d choreography
\i schema.sql
\i schema_routines.sql
\i schema_trackfeedback.sql
\i migrate_add_audience.sql
```

## Usage

### Run Complete Sync (Recommended)

Sync both tracks and routines in one command:

```bash
python sync_all.py
```

This will:
1. Sync all tracks from base44
2. Sync all routines from base44
3. Link routines to tracks automatically
4. Log all sync operations

### Run Individual Syncs

Sync only tracks:
```bash
python sync.py
```

Sync only routines:
```bash
python sync_routines.py
```

**Note:** Always sync tracks before routines, as routines reference tracks.

### Database Schema

#### `tracks` table
- Basic info: title, artist, album, duration
- Spotify integration: ID, album art, URL
- Musical characteristics: BPM, intensity, track type, focus area
- Cycling data: resistance/cadence ranges, base RPM, effort level
- Choreography: structured cues and notes (JSONB)
- Timestamps: synced_at, updated_at, created_at

#### `routines` table
- Basic info: name, description, theme, intensity arc
- Metadata: difficulty, total duration, class summary
- Integration: Spotify playlist ID
- Tags: JSONB array for categorization
- Timestamps: synced_at, updated_at, created_at

#### `routine_tracks` table (junction)
- Links routines to tracks in order
- `routine_id`: Reference to routine
- `track_base44_id`: Track identifier from base44
- `track_id`: Optional reference to local tracks table
- `track_order`: Position in routine sequence

#### `sync_log` table
- Tracks each sync operation with statistics and status

## Query Examples

Explore your data with the example queries:

- **[example_queries.sql](example_queries.sql)** - Track queries (BPM ranges, intensity, choreography cues)
- **[example_queries_routines.sql](example_queries_routines.sql)** - Routine queries (workout planning, track usage, stats)

## MCP Server

The server is a standard MCP server, so any MCP-compatible AI client can use it to build and refine cycling class playlists from your local database.

### Install MCP Dependencies

```bash
pip install -r requirements_mcp.txt
```

### Run Server Directly (STDIO)

```bash
python mcp_server.py --transport stdio
```

### Run Server Over SSE (HTTP)

```bash
python mcp_server.py --transport sse --host 0.0.0.0 --port 8000
```

Default SSE endpoints:
- Stream endpoint: `http://localhost:8000/sse`
- Message endpoint: `http://localhost:8000/messages/`

You can customize paths:

```bash
python mcp_server.py --transport sse --host 0.0.0.0 --port 8000 --sse-path /events --message-path /messages/
```

### Run Server Over Streamable HTTP

```bash
python mcp_server.py --transport streamable-http --host 0.0.0.0 --port 8000
```

Default endpoint:
- `http://localhost:8000/mcp`

### Run with Docker Compose

Use this when you want reproducible local services for MCP + Web API.

1. Create and fill your env file:

```bash
cp .env.example .env
# edit .env with your DB/API/OpenAI values
```

2. If you are using containerized Postgres, initialize and sync data first:

```bash
# Start Postgres only
docker compose up -d postgres

# Pull tracks/routines from base44 into Postgres
docker compose run --rm --profile sync sync
```

3. Build and start MCP + Web API:

```bash
docker compose up -d --build
```

4. Verify health:

```bash
curl http://localhost:8000/mcp
curl http://localhost:8080/health
```

5. Stop services:

```bash
docker compose down
```

Notes:
- The compose stack exposes:
  - MCP server: `http://localhost:${MCP_HOST_PORT:-8000}/mcp`
  - Web API: `http://localhost:${WEBAPI_HOST_PORT:-8080}`
- The Web API calls MCP internally using `http://mcp-server:8000/mcp`.
- Compose includes a `postgres` service and persists data in the `postgres_data` volume.
- The `sync` profile service runs `sync_all.py` on demand.
- DB credentials come from `.env` (`DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD`).
- If ports are busy, set host port overrides in `.env`, for example:
  - `MCP_HOST_PORT=18000`
  - `WEBAPI_HOST_PORT=18080`

### Protect MCP Server (Bearer Auth)

Set these environment variables before starting `mcp_server.py`:

```bash
MCP_AUTH_BEARER_TOKEN=replace_with_long_random_secret
MCP_AUTH_SCOPES=mcp:access
MCP_AUTH_ISSUER_URL=http://127.0.0.1:8000
MCP_AUTH_RESOURCE_URL=http://127.0.0.1:8000
```

When enabled, clients must send:

```http
Authorization: Bearer <MCP_AUTH_BEARER_TOKEN>
```

Local quick test:

```bash
curl -i http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer replace_with_long_random_secret"
```

### Generic MCP Client Config

Most MCP clients support a config shaped like this:

```json
{
  "mcpServers": {
    "choreography-db": {
      "command": "python",
      "args": ["/home/glen/Documents/Projects/cyclemcpserver/mcp_server.py", "--transport", "stdio"]
    }
  }
}
```

For network transports, set args for SSE or streamable HTTP instead of stdio:
- SSE: `["/home/glen/Documents/Projects/cyclemcpserver/mcp_server.py", "--transport", "sse", "--host", "0.0.0.0", "--port", "8000"]`
- Streamable HTTP: `["/home/glen/Documents/Projects/cyclemcpserver/mcp_server.py", "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "8000"]`

### Claude Code CLI (Example)

```bash
claude mcp add choreography-db -- python /home/glen/Documents/Projects/cyclemcpserver/mcp_server.py --transport stdio
```

### Claude Desktop (Example)

Edit your Claude Desktop config (`~/.config/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "choreography-db": {
      "command": "python",
      "args": ["/home/glen/Documents/Projects/cyclemcpserver/mcp_server.py", "--transport", "stdio"]
    }
  }
}
```

### Available Tools

| Tool | What It Does |
|------|--------------|
| `search_tracks` | Find tracks by BPM, intensity, type, position, artist, keyword |
| `suggest_tracks_for_slot` | Get best tracks for a class slot (warmup, climb, etc.) ranked by feedback |
| `find_similar_tracks` | Find alternatives to a track based on BPM and intensity |
| `get_track_details` | Full track info including choreography cues |
| `get_top_rated_tracks` | Tracks ranked by your feedback ratings |
| `get_feedback_summary` | Overview of all your track ratings by context |
| `build_class_playlist` | Auto-generate a full class playlist with proper workout arc |
| `build_hybrid_playlist` | Build full playlist using DB tracks first, then OpenAI gap-fill, filtered by feedback |
| `list_routines` | Browse your existing routines/classes |

### Example Queries

Once connected, ask your AI client things like:
- "Build me a 45-minute intermediate cycling class"
- "Find me high-energy climb tracks around 130 BPM"
- "What are my top-rated sprint tracks?"
- "Find tracks similar to 'Everybody Everybody' for a warmup slot"
- "Show me all my existing routines"

## Scheduling Syncs

Keep your database up to date with a cron job:

```bash
# Add to crontab: sync every day at 2am
0 2 * * * cd /path/to/cyclemcpserver && python sync_all.py >> sync.log 2>&1
```

## Troubleshooting

### Connection Issues

If you get database connection errors:
- Verify PostgreSQL is running: `systemctl status postgresql`
- Check your credentials in `.env`
- Ensure the database exists: `psql -l`

### API Issues

If base44 API calls fail:
- Verify your API key is correct
- Check the API endpoint URL (update in `.env` if needed)
- Review base44 API documentation for correct endpoint paths

### Field Mapping

The sync script assumes certain field names from the base44 API. If your tracks aren't syncing correctly, you may need to adjust the field mapping in `sync.py` in the `sync_track()` method based on the actual API response structure.

Run a test API call to see the structure:

```python
import requests
headers = {'Authorization': 'Bearer YOUR_API_KEY'}
response = requests.get('https://api.base44.com/tracks', headers=headers)
print(response.json())
```

Then update the field names in `sync_track()` accordingly.

## Service Wrapper (Start/Stop Both)

Use `manage_services.sh` to run both MCP server and Web API together.

```bash
# Start both services
./manage_services.sh start

# Check status
./manage_services.sh status

# View logs
./manage_services.sh logs
./manage_services.sh logs mcp
./manage_services.sh logs web

# Stop both services
./manage_services.sh stop
```

Default ports:
- MCP: `127.0.0.1:8000`
- Web API: `0.0.0.0:8080`

Override with env vars:

```bash
MCP_HOST=0.0.0.0 MCP_PORT=8000 WEBAPI_PORT=8081 ./manage_services.sh start
```

## Web API (MCP + OpenAI)

This repo includes a web backend (`webapp_api.py`) that:
- requires an `X-API-Key` header
- calls your MCP server for track suggestions
- optionally calls OpenAI to curate a final list from MCP suggestions
- returns a `routine` object aligned to `routine_schema.json`

### Install Web API Dependencies

```bash
pip install -r requirements_webapi.txt
```

### Required Environment Variables

Add these to `.env`:

```bash
# Protects /api/playlist
WEBAPP_API_KEY=replace_with_your_secret_key

# MCP server endpoint (streamable-http transport)
MCP_SERVER_URL=http://127.0.0.1:8000/mcp
MCP_SERVER_BEARER_TOKEN=replace_with_long_random_secret

# OpenAI
OPENAI_API_KEY=replace_with_openai_key
OPENAI_MODEL=gpt-4o-mini
```

### Start MCP Server (HTTP)

```bash
python mcp_server.py --transport streamable-http --host 127.0.0.1 --port 8000
```

### Start Web API

```bash
uvicorn webapp_api:app --host 0.0.0.0 --port 8080
```

### Test Endpoint

```bash
curl -X POST http://127.0.0.1:8080/api/playlist \
  -H "Content-Type: application/json" \
  -H "X-API-Key: replace_with_your_secret_key" \
  -d '{
    "duration_minutes": 45,
    "difficulty": "intermediate",
    "audience": "mixed",
    "theme": "high energy",
    "intensity_arc": "Warmup -> Build -> Peak -> Recovery -> Cooldown",
    "vibe": "empowerment",
    "preferred_genres": ["edm", "pop"],
    "preferred_artists": ["Dua Lipa"],
    "excluded_genres": ["country"],
    "user_goal": "Build a class with smooth transitions and a strong peak section."
  }'
```

Response is the routine object directly (schema-aligned).

To include debug internals (`mcp_playlist`, `openai`, request snapshot), set:
- `"debug": true`

`track_ids` precedence:
1. `spotify:{spotify_id}`
2. `base44:{base44_id}`
3. `db:{id}`
4. fallback slug if no IDs are present

### Track List Endpoint (TypeScript-style)

If your frontend wants a direct `tracks` payload similar to your TS function, call:

```bash
curl -X POST http://127.0.0.1:8080/api/tracks \
  -H "Content-Type: application/json" \
  -H "X-API-Key: replace_with_your_secret_key" \
  -d '{
    "duration_minutes": 45,
    "theme": "high energy",
    "intensity_arc": "Warmup -> Build -> Peak -> Recovery -> Cooldown",
    "vibe": "empowerment",
    "preferred_genres": ["edm", "pop"],
    "preferred_artists": ["Dua Lipa"],
    "excluded_genres": ["country"],
    "spotify_access_token": "optional_spotify_user_token"
  }'
```

Response:
- `{ "tracks": [...] }`
- If `spotify_access_token` is provided, returns Spotify-enriched track objects.
