#!/usr/bin/env python3
"""
MCP Server for Cycle Class Track Suggestions.

Connects to the local PostgreSQL choreography database and provides tools
for searching tracks, analyzing feedback, and building class playlists.
"""
import os
import sys
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, date

import requests
import psycopg2
from psycopg2.pool import SimpleConnectionPool
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Load .env from the same directory as this script
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

# Configure logging to stderr (stdout is reserved for STDIO transport)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger('choreography-mcp')


def serialize(obj):
    """Convert non-serializable types for JSON output."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    return obj


def serialize_rows(rows):
    """Serialize a list of RealDictRow results."""
    return [{k: serialize(v) for k, v in row.items()} for row in rows]


@dataclass
class AppContext:
    db_pool: SimpleConnectionPool
    base44_api_key: str
    base44_api_url: str
    base44_app_id: str


@asynccontextmanager
async def app_lifespan(server: FastMCP):
    """Manage database connection pool lifecycle."""
    db_config = {
        'host': os.getenv('DB_HOST', 'localhost'),
        'port': int(os.getenv('DB_PORT', '5432')),
        'database': os.getenv('DB_NAME', 'choreography'),
        'user': os.getenv('DB_USER'),
        'password': os.getenv('DB_PASSWORD'),
    }

    logger.info("Initializing database connection pool...")
    pool = SimpleConnectionPool(1, 10, **db_config)
    try:
        logger.info("MCP server ready.")
        yield AppContext(
            db_pool=pool,
            base44_api_key=os.getenv('BASE44_API_KEY', ''),
            base44_api_url=os.getenv('BASE44_API_URL', 'https://app.base44.com/api'),
            base44_app_id=os.getenv('BASE44_APP_ID', ''),
        )
    finally:
        pool.closeall()
        logger.info("Database pool closed.")


# Initialize FastMCP server
mcp = FastMCP("choreography-db", lifespan=app_lifespan)


def get_conn(ctx):
    """Get a connection from the pool."""
    return ctx.request_context.lifespan_context.db_pool.getconn()


def put_conn(ctx, conn):
    """Return a connection to the pool."""
    ctx.request_context.lifespan_context.db_pool.putconn(conn)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def search_tracks(
    ctx,
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    intensity: str | None = None,
    track_type: str | None = None,
    position: str | None = None,
    artist: str | None = None,
    focus_area: str | None = None,
    keyword: str | None = None,
    limit: int = 20,
) -> str:
    """Search for tracks by musical and cycling criteria.

    Use this to find tracks matching specific BPM ranges, intensity levels,
    track types (warmup, climb, sprint, etc.), riding positions, or artists.
    The keyword parameter searches across title, artist, album, and notes.

    Args:
        bpm_min: Minimum BPM (e.g. 100)
        bpm_max: Maximum BPM (e.g. 140)
        intensity: Filter by intensity level (low, medium, high, extreme)
        track_type: Filter by track type (warmup, climb, sprint, recovery, cooldown, intervals, endurance)
        position: Filter by riding position
        artist: Filter by artist name (partial match)
        focus_area: Filter by focus area (e.g. endurance, strength)
        keyword: Search across title, artist, album, and notes
        limit: Maximum results to return (default 20)
    """
    conn = get_conn(ctx)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        conditions = []
        params = []

        if bpm_min is not None:
            conditions.append("bpm >= %s")
            params.append(bpm_min)
        if bpm_max is not None:
            conditions.append("bpm <= %s")
            params.append(bpm_max)
        if intensity:
            conditions.append("intensity = %s")
            params.append(intensity)
        if track_type:
            conditions.append("track_type ILIKE %s")
            params.append(f"%{track_type}%")
        if position:
            conditions.append("position ILIKE %s")
            params.append(f"%{position}%")
        if artist:
            conditions.append("artist ILIKE %s")
            params.append(f"%{artist}%")
        if focus_area:
            conditions.append("focus_area ILIKE %s")
            params.append(f"%{focus_area}%")
        if keyword:
            conditions.append(
                "(title ILIKE %s OR artist ILIKE %s OR album ILIKE %s OR notes ILIKE %s)"
            )
            kw = f"%{keyword}%"
            params.extend([kw, kw, kw, kw])

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(min(limit, 50))

        cur.execute(f"""
            SELECT title, artist, album, bpm, intensity, track_type, focus_area,
                   position, duration_minutes, resistance_min, resistance_max,
                   cadence_min, cadence_max, base_rpm, base_effortlevel,
                   spotify_url
            FROM tracks
            {where}
            ORDER BY title
            LIMIT %s
        """, params)

        rows = serialize_rows(cur.fetchall())
        cur.close()
        return json.dumps(rows, indent=2)
    finally:
        put_conn(ctx, conn)


@mcp.tool()
def suggest_tracks_for_slot(
    ctx,
    slot_type: str,
    duration_min: float | None = None,
    duration_max: float | None = None,
    intensity: str | None = None,
    bpm_min: float | None = None,
    bpm_max: float | None = None,
    exclude_titles: str | None = None,
    audience: str | None = None,
    prefer_top_rated: bool = True,
    limit: int = 10,
) -> str:
    """Suggest the best tracks for a specific slot in a cycling class.

    Given a slot type (warmup, climb, sprint, recovery, cooldown, intervals,
    endurance), returns matching tracks ranked by feedback rating.
    When audience is specified, tracks rated thumbs-down for that audience
    are deprioritized, and thumbs-up tracks are boosted.

    Args:
        slot_type: The class slot type (warmup, climb, sprint, recovery, cooldown, intervals, endurance)
        duration_min: Minimum track duration in minutes
        duration_max: Maximum track duration in minutes
        intensity: Preferred intensity (low, medium, high, extreme)
        bpm_min: Minimum BPM
        bpm_max: Maximum BPM
        exclude_titles: Comma-separated track titles to exclude (already in playlist)
        audience: Target audience demographic (e.g. '50+', 'mixed', 'young') - filters out tracks rated down for this audience
        prefer_top_rated: Rank tracks with thumbs-up feedback higher (default true)
        limit: Maximum results (default 10)
    """
    conn = get_conn(ctx)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        conditions = ["track_type ILIKE %s"]
        params = [f"%{slot_type}%"]

        if duration_min is not None:
            conditions.append("duration_minutes >= %s")
            params.append(duration_min)
        if duration_max is not None:
            conditions.append("duration_minutes <= %s")
            params.append(duration_max)
        if intensity:
            conditions.append("intensity = %s")
            params.append(intensity)
        if bpm_min is not None:
            conditions.append("bpm >= %s")
            params.append(bpm_min)
        if bpm_max is not None:
            conditions.append("bpm <= %s")
            params.append(bpm_max)
        if exclude_titles:
            titles = [t.strip() for t in exclude_titles.split(',')]
            placeholders = ','.join(['%s'] * len(titles))
            conditions.append(f"t.title NOT IN ({placeholders})")
            params.extend(titles)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        all_params = list(params)
        all_params.append(min(limit, 50))

        order = """
            ORDER BY
                CASE WHEN fb.down_audience > 0 THEN 4
                     WHEN fb.up_audience > 0 AND fb.down_count = 0 THEN 0
                     WHEN fb.up_count > 0 AND fb.down_count = 0 THEN 1
                     WHEN fb.up_count > fb.down_count THEN 2
                     WHEN fb.up_count = 0 AND fb.down_count = 0 THEN 3
                     ELSE 4
                END,
                t.title
        """ if prefer_top_rated else "ORDER BY t.title"

        audience_cols = ""
        audience_sums = ""
        if audience:
            audience_cols = ", COALESCE(fb.up_audience, 0) as audience_thumbs_up, COALESCE(fb.down_audience, 0) as audience_thumbs_down"
            audience_sums = f""",
                       SUM(CASE WHEN rating = 'up' AND audience = %s THEN 1 ELSE 0 END) as up_audience,
                       SUM(CASE WHEN rating = 'down' AND audience = %s THEN 1 ELSE 0 END) as down_audience"""
            all_params = [audience, audience] + params
            all_params.append(min(limit, 50))
        else:
            audience_sums = """,
                       0 as up_audience,
                       0 as down_audience"""

        cur.execute(f"""
            SELECT t.title, t.artist, t.bpm, t.intensity, t.track_type,
                   t.duration_minutes, t.position, t.focus_area,
                   t.resistance_min, t.resistance_max,
                   t.cadence_min, t.cadence_max,
                   t.spotify_url,
                   COALESCE(fb.up_count, 0) as thumbs_up,
                   COALESCE(fb.down_count, 0) as thumbs_down
                   {audience_cols}
            FROM tracks t
            LEFT JOIN (
                SELECT track_title,
                       SUM(CASE WHEN rating = 'up' THEN 1 ELSE 0 END) as up_count,
                       SUM(CASE WHEN rating = 'down' THEN 1 ELSE 0 END) as down_count
                       {audience_sums}
                FROM track_feedback
                GROUP BY track_title
            ) fb ON fb.track_title = t.title
            {where}
            {order}
            LIMIT %s
        """, all_params)

        rows = serialize_rows(cur.fetchall())
        cur.close()
        return json.dumps(rows, indent=2)
    finally:
        put_conn(ctx, conn)


@mcp.tool()
def find_similar_tracks(
    ctx,
    track_title: str,
    bpm_tolerance: float = 15,
    limit: int = 10,
) -> str:
    """Find tracks similar to a given track based on BPM, intensity, and type.

    Useful for finding alternatives or substitutions for a track in a class.

    Args:
        track_title: The title of the reference track
        bpm_tolerance: How close the BPM should be (default 15)
        limit: Maximum results (default 10)
    """
    conn = get_conn(ctx)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Get the reference track
        cur.execute("SELECT * FROM tracks WHERE title ILIKE %s LIMIT 1", (f"%{track_title}%",))
        ref = cur.fetchone()

        if not ref:
            cur.close()
            return json.dumps({"error": f"Track '{track_title}' not found"})

        conditions = ["t.title != %s"]
        params = [ref['title']]

        if ref['bpm']:
            conditions.append("t.bpm BETWEEN %s AND %s")
            params.extend([float(ref['bpm']) - bpm_tolerance, float(ref['bpm']) + bpm_tolerance])

        if ref['intensity']:
            conditions.append("t.intensity = %s")
            params.append(ref['intensity'])

        where = f"WHERE {' AND '.join(conditions)}"
        params.append(min(limit, 50))

        cur.execute(f"""
            SELECT t.title, t.artist, t.bpm, t.intensity, t.track_type,
                   t.duration_minutes, t.position, t.focus_area,
                   t.spotify_url,
                   ABS(t.bpm - %s) as bpm_diff
            FROM tracks t
            {where}
            ORDER BY ABS(t.bpm - %s), t.title
            LIMIT %s
        """, [ref['bpm']] + params + [ref['bpm']])

        rows = serialize_rows(cur.fetchall())
        cur.close()

        return json.dumps({
            "reference_track": {
                "title": ref['title'],
                "artist": ref['artist'],
                "bpm": serialize(ref['bpm']),
                "intensity": ref['intensity'],
                "track_type": ref['track_type'],
            },
            "similar_tracks": rows,
        }, indent=2)
    finally:
        put_conn(ctx, conn)


@mcp.tool()
def get_track_details(ctx, track_title: str) -> str:
    """Get full details of a track including choreography cues.

    Returns all information about a track: metadata, cycling parameters,
    choreography breakdown, coaching cues, and notes.

    Args:
        track_title: The title of the track (partial match supported)
    """
    conn = get_conn(ctx)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(
            "SELECT * FROM tracks WHERE title ILIKE %s LIMIT 1",
            (f"%{track_title}%",)
        )
        row = cur.fetchone()
        cur.close()

        if not row:
            return json.dumps({"error": f"Track '{track_title}' not found"})

        result = {k: serialize(v) for k, v in row.items()}
        return json.dumps(result, indent=2)
    finally:
        put_conn(ctx, conn)


@mcp.tool()
def get_top_rated_tracks(
    ctx,
    context: str | None = None,
    audience: str | None = None,
    rating: str = "up",
    limit: int = 15,
) -> str:
    """Get tracks with the best feedback ratings.

    Shows tracks ranked by number of thumbs-up (or thumbs-down) ratings,
    optionally filtered by usage context and/or audience demographic.

    Args:
        context: Filter by usage context (e.g. warmup, climb, sprint)
        audience: Filter by audience demographic (e.g. '50+', 'mixed', 'young')
        rating: Rating type to rank by - 'up' or 'down' (default 'up')
        limit: Maximum results (default 15)
    """
    conn = get_conn(ctx)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        conditions = ["f.rating = %s"]
        params = [rating]

        if context:
            conditions.append("f.context ILIKE %s")
            params.append(f"%{context}%")
        if audience:
            conditions.append("f.audience = %s")
            params.append(audience)

        where = f"WHERE {' AND '.join(conditions)}"
        params.append(min(limit, 50))

        cur.execute(f"""
            SELECT f.track_title, f.track_artist, f.context, f.audience, f.rating,
                   COUNT(*) as rating_count,
                   t.bpm, t.intensity, t.track_type, t.duration_minutes,
                   t.spotify_url
            FROM track_feedback f
            LEFT JOIN tracks t ON t.title = f.track_title
            {where}
            GROUP BY f.track_title, f.track_artist, f.context, f.audience, f.rating,
                     t.bpm, t.intensity, t.track_type, t.duration_minutes,
                     t.spotify_url
            ORDER BY COUNT(*) DESC, f.track_title
            LIMIT %s
        """, params)

        rows = serialize_rows(cur.fetchall())
        cur.close()
        return json.dumps(rows, indent=2)
    finally:
        put_conn(ctx, conn)


@mcp.tool()
def get_feedback_summary(ctx) -> str:
    """Get a summary of all track feedback grouped by context and rating.

    Returns an overview of how many tracks have been rated up/down
    in each context (warmup, climb, sprint, etc.), plus overall stats.
    """
    conn = get_conn(ctx)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Overall stats
        cur.execute("""
            SELECT
                COUNT(*) as total_feedback,
                SUM(CASE WHEN rating = 'up' THEN 1 ELSE 0 END) as total_up,
                SUM(CASE WHEN rating = 'down' THEN 1 ELSE 0 END) as total_down,
                COUNT(DISTINCT track_title) as unique_tracks
            FROM track_feedback
        """)
        overall = serialize_rows(cur.fetchall())[0]

        # By context
        cur.execute("""
            SELECT
                COALESCE(context, 'unspecified') as context,
                SUM(CASE WHEN rating = 'up' THEN 1 ELSE 0 END) as up_count,
                SUM(CASE WHEN rating = 'down' THEN 1 ELSE 0 END) as down_count,
                COUNT(DISTINCT track_title) as unique_tracks
            FROM track_feedback
            GROUP BY context
            ORDER BY COUNT(*) DESC
        """)
        by_context = serialize_rows(cur.fetchall())
        cur.close()

        return json.dumps({
            "overall": overall,
            "by_context": by_context,
        }, indent=2)
    finally:
        put_conn(ctx, conn)


@mcp.tool()
def build_class_playlist(
    ctx,
    duration_minutes: int = 45,
    difficulty: str | None = None,
    theme: str | None = None,
    audience: str | None = None,
) -> str:
    """Build a suggested cycling class playlist with a proper workout arc.

    Generates a class structure with tracks for each phase:
    warmup → building → peak (climbs/sprints/intervals) → recovery → cooldown.
    Prefers top-rated tracks and avoids duplicates.
    When audience is specified, tracks rated thumbs-down for that audience are excluded.

    Args:
        duration_minutes: Target class duration in minutes (default 45)
        difficulty: Preferred difficulty - affects intensity selection
        theme: Optional theme or focus (e.g. 'climb heavy', 'high energy', 'endurance')
        audience: Target audience demographic (e.g. '50+', 'mixed', 'young') - excludes tracks rated down for this audience
    """
    conn = get_conn(ctx)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Define class structure based on duration
        if duration_minutes <= 30:
            structure = [
                {"phase": "Warmup", "types": ["warmup"], "count": 1},
                {"phase": "Build", "types": ["endurance", "intervals"], "count": 2},
                {"phase": "Peak", "types": ["climb", "sprint", "intervals"], "count": 2},
                {"phase": "Cooldown", "types": ["cooldown", "recovery"], "count": 1},
            ]
        elif duration_minutes <= 45:
            structure = [
                {"phase": "Warmup", "types": ["warmup"], "count": 1},
                {"phase": "Build", "types": ["endurance", "intervals"], "count": 2},
                {"phase": "Peak 1", "types": ["climb", "sprint"], "count": 2},
                {"phase": "Recovery", "types": ["recovery"], "count": 1},
                {"phase": "Peak 2", "types": ["climb", "sprint", "intervals"], "count": 2},
                {"phase": "Cooldown", "types": ["cooldown", "recovery"], "count": 1},
            ]
        else:
            structure = [
                {"phase": "Warmup", "types": ["warmup"], "count": 2},
                {"phase": "Build", "types": ["endurance", "intervals"], "count": 2},
                {"phase": "Peak 1", "types": ["climb", "sprint"], "count": 2},
                {"phase": "Active Recovery", "types": ["recovery", "endurance"], "count": 1},
                {"phase": "Peak 2", "types": ["climb", "sprint", "intervals"], "count": 2},
                {"phase": "Recovery", "types": ["recovery"], "count": 1},
                {"phase": "Peak 3", "types": ["climb", "sprint"], "count": 1},
                {"phase": "Cooldown", "types": ["cooldown", "recovery"], "count": 1},
            ]

        # Map difficulty to intensity preferences
        intensity_map = {
            "beginner": ["low", "medium"],
            "intermediate": ["medium", "high"],
            "advanced": ["high", "extreme"],
            "expert": ["high", "extreme"],
        }
        preferred_intensities = intensity_map.get(difficulty, []) if difficulty else []

        used_titles = set()
        playlist = []

        for slot in structure:
            type_placeholders = ','.join(['%s'] * len(slot['types']))
            params = []

            # Audience-aware feedback subquery params come first
            audience_sums = ""
            audience_order = ""
            if audience:
                audience_sums = """,
                           SUM(CASE WHEN rating = 'down' AND audience = %s THEN 1 ELSE 0 END) as down_audience"""
                audience_order = "CASE WHEN COALESCE(fb.down_audience, 0) > 0 THEN -1 ELSE 0 END DESC,"
                params.append(audience)

            params.extend(slot['types'])

            intensity_clause = ""
            if preferred_intensities:
                int_placeholders = ','.join(['%s'] * len(preferred_intensities))
                intensity_clause = f"AND t.intensity IN ({int_placeholders})"
                params.extend(preferred_intensities)

            theme_clause = ""
            if theme:
                theme_clause = "AND (t.notes ILIKE %s OR t.focus_area ILIKE %s)"
                params.extend([f"%{theme}%", f"%{theme}%"])

            # Exclude already-used tracks
            exclude_clause = ""
            if used_titles:
                exclude_placeholders = ','.join(['%s'] * len(used_titles))
                exclude_clause = f"AND t.title NOT IN ({exclude_placeholders})"
                params.extend(list(used_titles))

            params.append(slot['count'])

            cur.execute(f"""
                SELECT t.title, t.artist, t.bpm, t.intensity, t.track_type,
                       t.duration_minutes, t.position, t.focus_area,
                       t.resistance_min, t.resistance_max,
                       t.cadence_min, t.cadence_max,
                       t.spotify_url,
                       COALESCE(fb.up_count, 0) as thumbs_up,
                       COALESCE(fb.down_count, 0) as thumbs_down
                FROM tracks t
                LEFT JOIN (
                    SELECT track_title,
                           SUM(CASE WHEN rating = 'up' THEN 1 ELSE 0 END) as up_count,
                           SUM(CASE WHEN rating = 'down' THEN 1 ELSE 0 END) as down_count
                           {audience_sums}
                    FROM track_feedback
                    GROUP BY track_title
                ) fb ON fb.track_title = t.title
                WHERE t.track_type ILIKE ANY(ARRAY[{type_placeholders}])
                {intensity_clause}
                {theme_clause}
                {exclude_clause}
                ORDER BY
                    {audience_order}
                    COALESCE(fb.up_count, 0) DESC,
                    RANDOM()
                LIMIT %s
            """, params)

            tracks = serialize_rows(cur.fetchall())
            for t in tracks:
                used_titles.add(t['title'])

            playlist.append({
                "phase": slot['phase'],
                "suggested_types": slot['types'],
                "tracks": tracks,
            })

        cur.close()

        # Calculate total duration
        total = sum(
            t.get('duration_minutes', 0) or 0
            for phase in playlist
            for t in phase['tracks']
        )

        return json.dumps({
            "target_duration": duration_minutes,
            "estimated_duration": round(total, 1),
            "difficulty": difficulty,
            "theme": theme,
            "playlist": playlist,
        }, indent=2)
    finally:
        put_conn(ctx, conn)


@mcp.tool()
def list_routines(
    ctx,
    difficulty: str | None = None,
    limit: int = 20,
) -> str:
    """List existing cycling routines/classes with their track counts.

    Shows saved routines from base44 with key metadata.

    Args:
        difficulty: Filter by difficulty (beginner, intermediate, advanced, expert)
        limit: Maximum results (default 20)
    """
    conn = get_conn(ctx)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        conditions = []
        params = []

        if difficulty:
            conditions.append("r.difficulty = %s")
            params.append(difficulty)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(min(limit, 50))

        cur.execute(f"""
            SELECT r.name, r.description, r.theme, r.intensity_arc,
                   r.difficulty, r.total_duration_minutes,
                   r.class_summary, r.tags,
                   r.spotify_playlist_id,
                   COUNT(rt.id) as track_count
            FROM routines r
            LEFT JOIN routine_tracks rt ON r.id = rt.routine_id
            {where}
            GROUP BY r.id, r.name, r.description, r.theme, r.intensity_arc,
                     r.difficulty, r.total_duration_minutes, r.class_summary,
                     r.tags, r.spotify_playlist_id
            ORDER BY r.name
            LIMIT %s
        """, params)

        rows = serialize_rows(cur.fetchall())
        cur.close()
        return json.dumps(rows, indent=2)
    finally:
        put_conn(ctx, conn)


@mcp.tool()
def rate_track(
    ctx,
    track_title: str,
    rating: str,
    context: str | None = None,
    audience: str | None = None,
) -> str:
    """Rate a track with thumbs up or down. Saves to local database AND syncs to base44.

    Use this after reviewing a track suggestion to record whether it's a good
    fit. Ratings influence future suggestions - thumbs-up tracks get prioritized.

    Args:
        track_title: The exact title of the track to rate
        rating: 'up' for thumbs up, 'down' for thumbs down
        context: Usage context (e.g. warmup, climb, sprint, recovery, cooldown)
        audience: Target audience demographic (e.g. '50+', 'mixed', 'young')
    """
    if rating not in ('up', 'down'):
        return json.dumps({"error": "Rating must be 'up' or 'down'"})

    conn = get_conn(ctx)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Look up the track to get artist and spotify_id
        cur.execute(
            "SELECT title, artist, spotify_id FROM tracks WHERE title ILIKE %s LIMIT 1",
            (f"%{track_title}%",)
        )
        track = cur.fetchone()

        if not track:
            cur.close()
            return json.dumps({"error": f"Track '{track_title}' not found in database"})

        track_title_exact = track['title']
        track_artist = track['artist']
        spotify_id = track['spotify_id']

        # Insert into local database
        cur.execute("""
            INSERT INTO track_feedback (
                track_title, track_artist, spotify_id,
                rating, context, audience, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (track_title, rating, COALESCE(context, ''), COALESCE(audience, ''))
            DO UPDATE SET updated_at = CURRENT_TIMESTAMP
            RETURNING id
        """, (track_title_exact, track_artist, spotify_id, rating, context, audience))

        feedback_id = cur.fetchone()['id']
        conn.commit()

        # Sync to base44
        app_ctx = ctx.request_context.lifespan_context
        base44_result = None
        if app_ctx.base44_api_key and app_ctx.base44_app_id:
            try:
                response = requests.post(
                    f"{app_ctx.base44_api_url}/apps/{app_ctx.base44_app_id}/entities/TrackFeedback",
                    headers={
                        'api_key': app_ctx.base44_api_key,
                        'Content-Type': 'application/json',
                    },
                    json={
                        'track_title': track_title_exact,
                        'track_artist': track_artist,
                        'spotify_id': spotify_id,
                        'rating': rating,
                        'context': context or '',
                    },
                    timeout=10,
                )
                response.raise_for_status()
                base44_result = "synced"
            except Exception as e:
                logger.error(f"Failed to sync feedback to base44: {e}")
                base44_result = f"failed: {e}"

        cur.close()

        emoji = "\U0001f44d" if rating == "up" else "\U0001f44e"
        return json.dumps({
            "status": "saved",
            "feedback_id": feedback_id,
            "track": track_title_exact,
            "artist": track_artist,
            "rating": f"{emoji} {rating}",
            "context": context,
            "audience": audience,
            "base44_sync": base44_result or "skipped (no API credentials)",
        }, indent=2)
    except Exception as e:
        conn.rollback()
        return json.dumps({"error": f"Failed to save rating: {e}"})
    finally:
        put_conn(ctx, conn)


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@mcp.resource("stats://tracks")
def track_stats() -> str:
    """Summary statistics of available tracks by type, intensity, and BPM ranges."""
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
    conn = psycopg2.connect(
        host=os.getenv('DB_HOST', 'localhost'),
        port=int(os.getenv('DB_PORT', '5432')),
        database=os.getenv('DB_NAME', 'choreography'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD'),
    )
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("SELECT COUNT(*) as total FROM tracks")
        total = cur.fetchone()['total']

        cur.execute("""
            SELECT track_type, COUNT(*) as count,
                   ROUND(AVG(bpm)::numeric, 1) as avg_bpm,
                   ROUND(MIN(bpm)::numeric, 1) as min_bpm,
                   ROUND(MAX(bpm)::numeric, 1) as max_bpm
            FROM tracks
            WHERE track_type IS NOT NULL
            GROUP BY track_type ORDER BY count DESC
        """)
        by_type = serialize_rows(cur.fetchall())

        cur.execute("""
            SELECT intensity, COUNT(*) as count
            FROM tracks WHERE intensity IS NOT NULL
            GROUP BY intensity ORDER BY count DESC
        """)
        by_intensity = serialize_rows(cur.fetchall())

        cur.execute("""
            SELECT ROUND(MIN(bpm)::numeric, 1) as min_bpm,
                   ROUND(MAX(bpm)::numeric, 1) as max_bpm,
                   ROUND(AVG(bpm)::numeric, 1) as avg_bpm
            FROM tracks WHERE bpm IS NOT NULL
        """)
        bpm_range = serialize_rows(cur.fetchall())[0]

        cur.close()
        return json.dumps({
            "total_tracks": total,
            "by_track_type": by_type,
            "by_intensity": by_intensity,
            "bpm_range": bpm_range,
        }, indent=2)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

@mcp.prompt()
def build_class(duration: str = "45", difficulty: str = "intermediate", audience: str = "50+") -> str:
    """Template prompt for building a cycling class playlist.

    Args:
        duration: Target duration in minutes
        difficulty: Difficulty level (beginner, intermediate, advanced, expert)
        audience: Target audience demographic (e.g. '50+', 'mixed', 'young')
    """
    return f"""Help me build a {duration}-minute cycling class at {difficulty} difficulty for a {audience} audience.

Please follow these steps:
1. First, use the track_stats resource to understand what tracks are available
2. Use build_class_playlist with audience='{audience}' to generate an initial playlist suggestion
3. Review the suggestions and use suggest_tracks_for_slot with audience='{audience}' to find alternatives if any slot needs improvement
4. Check get_top_rated_tracks with audience='{audience}' to see if any highly-rated tracks were missed
5. Present the final playlist in order with:
   - Phase/slot name
   - Track title and artist
   - BPM and intensity
   - Duration
   - Any relevant choreography notes
6. After presenting the playlist, ask if I want to rate any tracks with thumbs up/down using rate_track (with audience='{audience}')

Consider:
- Smooth BPM transitions between consecutive tracks
- Proper intensity arc (build up → peak → recover → peak → cool down)
- Mix of positions and resistance levels for variety
- Prefer tracks with positive feedback ratings
- Exclude tracks previously rated thumbs-down for this audience
- Songs that resonate with a {audience} demographic
"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run(transport="stdio")
