# base44 → PostgreSQL Sync

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
```

Or connect to your database and run the schemas manually:

```bash
psql -h localhost -U your_db_user -d choreography
\i schema.sql
\i schema_routines.sql
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

## Next Steps

Once you have tracks and routines synced, you can:

1. **Set up an MCP server** to query and analyze your choreography database
2. **Build workout plans** by querying routines by difficulty, duration, or theme
3. **Analyze track usage** across routines to identify popular tracks
4. **Query choreography cues** to refine your class structure
5. **Schedule regular syncs** using cron or systemd timers:
   ```bash
   # Add to crontab: sync every day at 2am
   0 2 * * * cd /path/to/base44sync && python sync_all.py >> sync.log 2>&1
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
