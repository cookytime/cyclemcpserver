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
import argparse
import hmac
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from datetime import datetime, date
from typing import Any

import requests
import psycopg2
from psycopg2.pool import SimpleConnectionPool
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP, Context
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings

# Load .env from the same directory as this script
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# Log level from env (DEBUG, INFO, WARNING, ERROR, CRITICAL)
LOG_LEVEL = os.getenv("MCP_LOG_LEVEL", "INFO").upper()
if LOG_LEVEL not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
    LOG_LEVEL = "INFO"

# Configure logging to stderr (stdout is reserved for STDIO transport)
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("choreography-mcp")


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


class StaticBearerTokenVerifier:
    """Simple bearer-token verifier for MCP HTTP transports."""

    def __init__(self, expected_token: str, client_id: str, scopes: list[str]):
        self.expected_token = expected_token
        self.client_id = client_id
        self.scopes = scopes

    async def verify_token(self, token: str) -> AccessToken | None:
        if not self.expected_token:
            return None
        if not hmac.compare_digest(token, self.expected_token):
            return None
        return AccessToken(
            token=token,
            client_id=self.client_id,
            scopes=self.scopes,
            expires_at=None,
            resource=None,
        )


@asynccontextmanager
async def app_lifespan(server: FastMCP):
    """Manage database connection pool lifecycle."""
    db_config = {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
        "database": os.getenv("DB_NAME", "choreography"),
        "user": os.getenv("DB_USER"),
        "password": os.getenv("DB_PASSWORD"),
        "connect_timeout": int(os.getenv("DB_CONNECT_TIMEOUT", "5")),
    }

    logger.info(
        "Initializing database connection pool (host=%s port=%s db=%s user=%s)...",
        db_config["host"],
        db_config["port"],
        db_config["database"],
        db_config["user"] or "<unset>",
    )
    try:
        pool = SimpleConnectionPool(1, 10, **db_config)
    except Exception as e:
        logger.error(
            "Database connection failed. Check DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD and that PostgreSQL is reachable."
        )
        raise RuntimeError("Failed to initialize PostgreSQL connection pool") from e
    try:
        logger.info("MCP server ready.")
        yield AppContext(
            db_pool=pool,
            base44_api_key=os.getenv("BASE44_API_KEY", ""),
            base44_api_url=os.getenv("BASE44_API_URL", "https://app.base44.com/api"),
            base44_app_id=os.getenv("BASE44_APP_ID", ""),
        )
    finally:
        pool.closeall()
        logger.info("Database pool closed.")


# Optional MCP HTTP auth
_auth_token = os.getenv("MCP_AUTH_BEARER_TOKEN", "").strip()
_auth_scopes = [
    s.strip()
    for s in os.getenv("MCP_AUTH_SCOPES", "mcp:access").split(",")
    if s.strip()
]
_auth_issuer_url = os.getenv("MCP_AUTH_ISSUER_URL", "http://127.0.0.1:8000")
_auth_resource_url = os.getenv("MCP_AUTH_RESOURCE_URL", "http://127.0.0.1:8000")

_auth_settings = None
_token_verifier = None
if _auth_token:
    _auth_settings = AuthSettings(
        issuer_url=_auth_issuer_url,
        resource_server_url=_auth_resource_url,
        required_scopes=_auth_scopes,
    )
    _token_verifier = StaticBearerTokenVerifier(
        expected_token=_auth_token,
        client_id=os.getenv("MCP_AUTH_CLIENT_ID", "authorized-client"),
        scopes=_auth_scopes,
    )
    logger.info(
        "MCP HTTP auth enabled (Bearer token + scopes=%s).", ",".join(_auth_scopes)
    )

# Initialize FastMCP server
mcp = FastMCP(
    "choreography-db",
    lifespan=app_lifespan,
    log_level=LOG_LEVEL,
    host=os.getenv("MCP_HOST", "127.0.0.1"),
    port=int(os.getenv("MCP_PORT", "8000")),
    mount_path=os.getenv("MCP_MOUNT_PATH", "/"),
    sse_path=os.getenv("MCP_SSE_PATH", "/sse"),
    message_path=os.getenv("MCP_MESSAGE_PATH", "/messages/"),
    streamable_http_path=os.getenv("MCP_HTTP_PATH", "/mcp"),
    auth=_auth_settings,
    token_verifier=_token_verifier,
)


def get_conn(ctx: Context):
    """Get a connection from the pool."""
    return ctx.request_context.lifespan_context.db_pool.getconn()


def put_conn(ctx: Context, conn):
    """Return a connection to the pool."""
    ctx.request_context.lifespan_context.db_pool.putconn(conn)


def normalize_track_key(title: str | None, artist: str | None) -> str:
    t = (title or "").strip().lower()
    a = (artist or "").strip().lower()
    return f"{t}|{a}"


def derive_target_track_count(
    duration_minutes: int, explicit_target: int | None
) -> int:
    if explicit_target is not None:
        return max(5, min(30, explicit_target))
    if duration_minutes <= 30:
        return 10
    if duration_minutes <= 45:
        return 12
    return 15


def fetch_feedback_signals(conn, audience: str | None = None) -> dict[str, list[str]]:
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        if audience:
            cur.execute(
                """
                SELECT track_title, track_artist, rating
                FROM track_feedback
                WHERE audience = %s OR audience IS NULL OR audience = ''
                """,
                (audience,),
            )
        else:
            cur.execute("SELECT track_title, track_artist, rating FROM track_feedback")
        rows = cur.fetchall()
    finally:
        cur.close()

    liked_titles: set[str] = set()
    liked_artists: set[str] = set()
    disliked_titles: set[str] = set()
    disliked_artists: set[str] = set()

    for row in rows:
        title = (row.get("track_title") or "").strip()
        artist = (row.get("track_artist") or "").strip()
        rating = (row.get("rating") or "").strip().lower()
        if rating == "up":
            if title:
                liked_titles.add(title)
            if artist:
                liked_artists.add(artist)
        elif rating == "down":
            if title:
                disliked_titles.add(title)
            if artist:
                disliked_artists.add(artist)

    return {
        "liked_titles": sorted(liked_titles),
        "liked_artists": sorted(liked_artists),
        "disliked_titles": sorted(disliked_titles),
        "disliked_artists": sorted(disliked_artists),
    }


def suggest_external_tracks_with_openai(
    duration_minutes: int,
    difficulty: str | None,
    theme: str | None,
    audience: str | None,
    needed_count: int,
    existing_tracks: list[dict[str, Any]],
    feedback_signals: dict[str, list[str]],
) -> list[dict[str, Any]]:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or needed_count <= 0:
        return []

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    timeout = int(os.getenv("OPENAI_TIMEOUT_SECONDS", "45"))

    system_prompt = (
        "You are an expert cycling class music programmer. "
        "Use existing tracks as anchors and suggest only missing tracks to complete a full class arc. "
        "Never suggest disliked tracks or disliked artists. "
        "Return valid JSON only."
    )
    user_payload = {
        "target_duration_minutes": duration_minutes,
        "difficulty": difficulty,
        "theme": theme,
        "audience": audience,
        "needed_count": needed_count,
        "existing_tracks": existing_tracks,
        "feedback_signals": feedback_signals,
        "required_output_format": {
            "tracks": [
                {
                    "title": "string",
                    "artist": "string",
                    "estimated_bpm": 120,
                    "focus_area": "warmup|build|climb|sprint|recovery|cooldown",
                    "notes": "string",
                }
            ]
        },
    }

    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "response_format": {"type": "json_object"},
            "temperature": 0.3,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user_payload)},
            ],
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    content = payload["choices"][0]["message"]["content"]
    parsed = json.loads(content)

    if isinstance(parsed, list):
        tracks = parsed
    elif isinstance(parsed, dict) and isinstance(parsed.get("tracks"), list):
        tracks = parsed["tracks"]
    else:
        tracks = []

    clean: list[dict[str, Any]] = []
    for item in tracks:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        artist = str(item.get("artist") or "").strip()
        if not title or not artist:
            continue
        clean.append(
            {
                "title": title,
                "artist": artist,
                "estimated_bpm": item.get("estimated_bpm"),
                "focus_area": str(item.get("focus_area") or "build").strip().lower(),
                "notes": str(item.get("notes") or "").strip(),
            }
        )
    return clean


def focus_area_to_phase_name(focus_area: str) -> str:
    value = (focus_area or "").lower()
    if "warm" in value:
        return "Warmup"
    if "cool" in value:
        return "Cooldown"
    if "recover" in value:
        return "Recovery"
    if "sprint" in value:
        return "Peak 2"
    if "climb" in value:
        return "Peak 1"
    if "build" in value or "endur" in value or "interval" in value:
        return "Build"
    return "Build"


def add_track_to_phase(
    playlist: list[dict[str, Any]], phase_name: str, track: dict[str, Any]
) -> None:
    for phase in playlist:
        if phase.get("phase") == phase_name:
            phase.setdefault("tracks", []).append(track)
            return
    playlist.append({"phase": phase_name, "suggested_types": [], "tracks": [track]})


def parse_csv_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def infer_default_arc(duration_minutes: int) -> list[str]:
    if duration_minutes <= 30:
        return ["warmup", "build", "climb", "sprint", "cooldown"]
    if duration_minutes <= 45:
        return ["warmup", "build", "climb", "recovery", "sprint", "cooldown"]
    return ["warmup", "build", "climb", "recovery", "climb", "sprint", "cooldown"]


def normalize_arc_types(
    custom_intensity_arc: str | None, duration_minutes: int
) -> list[str]:
    provided = [s.lower() for s in parse_csv_list(custom_intensity_arc)]
    if not provided:
        return infer_default_arc(duration_minutes)

    normalized: list[str] = []
    for item in provided:
        if "warm" in item:
            normalized.append("warmup")
        elif "cool" in item:
            normalized.append("cooldown")
        elif "recover" in item:
            normalized.append("recovery")
        elif "sprint" in item:
            normalized.append("sprint")
        elif "climb" in item:
            normalized.append("climb")
        elif "build" in item or "endur" in item or "interval" in item:
            normalized.append("build")
        else:
            normalized.append("build")
    return normalized


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def search_tracks(
    ctx: Context,
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

        cur.execute(
            f"""
            SELECT title, artist, album, bpm, intensity, track_type, focus_area,
                   position, duration_minutes, resistance_min, resistance_max,
                   cadence_min, cadence_max, base_rpm, base_effortlevel,
                   spotify_url
            FROM tracks
            {where}
            ORDER BY title
            LIMIT %s
        """,
            params,
        )

        rows = serialize_rows(cur.fetchall())
        cur.close()
        return json.dumps(rows, indent=2)
    finally:
        put_conn(ctx, conn)


@mcp.tool()
def suggest_tracks_for_slot(
    ctx: Context,
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
            titles = [t.strip() for t in exclude_titles.split(",")]
            placeholders = ",".join(["%s"] * len(titles))
            conditions.append(f"t.title NOT IN ({placeholders})")
            params.extend(titles)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        all_params = list(params)
        all_params.append(min(limit, 50))

        order = (
            """
            ORDER BY
                CASE WHEN fb.down_audience > 0 THEN 4
                     WHEN fb.up_audience > 0 AND fb.down_count = 0 THEN 0
                     WHEN fb.up_count > 0 AND fb.down_count = 0 THEN 1
                     WHEN fb.up_count > fb.down_count THEN 2
                     WHEN fb.up_count = 0 AND fb.down_count = 0 THEN 3
                     ELSE 4
                END,
                t.title
        """
            if prefer_top_rated
            else "ORDER BY t.title"
        )

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

        cur.execute(
            f"""
            SELECT t.id, t.spotify_id,
                   t.title, t.artist, t.bpm, t.intensity, t.track_type,
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
        """,
            all_params,
        )

        rows = serialize_rows(cur.fetchall())
        cur.close()
        return json.dumps(rows, indent=2)
    finally:
        put_conn(ctx, conn)


@mcp.tool()
def find_similar_tracks(
    ctx: Context,
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
        cur.execute(
            "SELECT * FROM tracks WHERE title ILIKE %s LIMIT 1", (f"%{track_title}%",)
        )
        ref = cur.fetchone()

        if not ref:
            cur.close()
            return json.dumps({"error": f"Track '{track_title}' not found"})

        conditions = ["t.title != %s"]
        params = [ref["title"]]

        if ref["bpm"]:
            conditions.append("t.bpm BETWEEN %s AND %s")
            params.extend(
                [float(ref["bpm"]) - bpm_tolerance, float(ref["bpm"]) + bpm_tolerance]
            )

        if ref["intensity"]:
            conditions.append("t.intensity = %s")
            params.append(ref["intensity"])

        where = f"WHERE {' AND '.join(conditions)}"
        safe_limit = min(limit, 50)

        cur.execute(
            f"""
            SELECT t.title, t.artist, t.bpm, t.intensity, t.track_type,
                   t.duration_minutes, t.position, t.focus_area,
                   t.spotify_url,
                   ABS(t.bpm - %s) as bpm_diff
            FROM tracks t
            {where}
            ORDER BY ABS(t.bpm - %s), t.title
            LIMIT %s
        """,
            [ref["bpm"]] + params + [ref["bpm"], safe_limit],
        )

        rows = serialize_rows(cur.fetchall())
        cur.close()

        return json.dumps(
            {
                "reference_track": {
                    "title": ref["title"],
                    "artist": ref["artist"],
                    "bpm": serialize(ref["bpm"]),
                    "intensity": ref["intensity"],
                    "track_type": ref["track_type"],
                },
                "similar_tracks": rows,
            },
            indent=2,
        )
    finally:
        put_conn(ctx, conn)


@mcp.tool()
def get_track_details(ctx: Context, track_title: str) -> str:
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
            "SELECT * FROM tracks WHERE title ILIKE %s LIMIT 1", (f"%{track_title}%",)
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
    ctx: Context,
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

        cur.execute(
            f"""
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
        """,
            params,
        )

        rows = serialize_rows(cur.fetchall())
        cur.close()
        return json.dumps(rows, indent=2)
    finally:
        put_conn(ctx, conn)


@mcp.tool()
def get_feedback_summary(ctx: Context) -> str:
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

        return json.dumps(
            {
                "overall": overall,
                "by_context": by_context,
            },
            indent=2,
        )
    finally:
        put_conn(ctx, conn)


@mcp.tool()
def build_class_playlist(
    ctx: Context,
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
                {
                    "phase": "Peak",
                    "types": ["climb", "sprint", "intervals"],
                    "count": 2,
                },
                {"phase": "Cooldown", "types": ["cooldown", "recovery"], "count": 1},
            ]
        elif duration_minutes <= 45:
            structure = [
                {"phase": "Warmup", "types": ["warmup"], "count": 1},
                {"phase": "Build", "types": ["endurance", "intervals"], "count": 2},
                {"phase": "Peak 1", "types": ["climb", "sprint"], "count": 2},
                {"phase": "Recovery", "types": ["recovery"], "count": 1},
                {
                    "phase": "Peak 2",
                    "types": ["climb", "sprint", "intervals"],
                    "count": 2,
                },
                {"phase": "Cooldown", "types": ["cooldown", "recovery"], "count": 1},
            ]
        else:
            structure = [
                {"phase": "Warmup", "types": ["warmup"], "count": 2},
                {"phase": "Build", "types": ["endurance", "intervals"], "count": 2},
                {"phase": "Peak 1", "types": ["climb", "sprint"], "count": 2},
                {
                    "phase": "Active Recovery",
                    "types": ["recovery", "endurance"],
                    "count": 1,
                },
                {
                    "phase": "Peak 2",
                    "types": ["climb", "sprint", "intervals"],
                    "count": 2,
                },
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
            type_placeholders = ",".join(["%s"] * len(slot["types"]))
            params = []

            # Audience-aware feedback subquery params come first
            audience_sums = ""
            audience_order = ""
            if audience:
                audience_sums = """,
                           SUM(CASE WHEN rating = 'down' AND audience = %s THEN 1 ELSE 0 END) as down_audience"""
                audience_order = "CASE WHEN COALESCE(fb.down_audience, 0) > 0 THEN -1 ELSE 0 END DESC,"
                params.append(audience)

            params.extend(slot["types"])

            intensity_clause = ""
            if preferred_intensities:
                int_placeholders = ",".join(["%s"] * len(preferred_intensities))
                intensity_clause = f"AND t.intensity IN ({int_placeholders})"
                params.extend(preferred_intensities)

            theme_clause = ""
            if theme:
                theme_clause = "AND (t.notes ILIKE %s OR t.focus_area ILIKE %s)"
                params.extend([f"%{theme}%", f"%{theme}%"])

            # Exclude already-used tracks
            exclude_clause = ""
            if used_titles:
                exclude_placeholders = ",".join(["%s"] * len(used_titles))
                exclude_clause = f"AND t.title NOT IN ({exclude_placeholders})"
                params.extend(list(used_titles))

            params.append(slot["count"])

            cur.execute(
                f"""
                SELECT t.id, t.spotify_id,
                       t.title, t.artist, t.bpm, t.intensity, t.track_type,
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
            """,
                params,
            )

            tracks = serialize_rows(cur.fetchall())
            for t in tracks:
                used_titles.add(t["title"])

            playlist.append(
                {
                    "phase": slot["phase"],
                    "suggested_types": slot["types"],
                    "tracks": tracks,
                }
            )

        cur.close()

        # Calculate total duration
        total = sum(
            t.get("duration_minutes", 0) or 0
            for phase in playlist
            for t in phase["tracks"]
        )

        return json.dumps(
            {
                "target_duration": duration_minutes,
                "estimated_duration": round(total, 1),
                "difficulty": difficulty,
                "theme": theme,
                "playlist": playlist,
            },
            indent=2,
        )
    finally:
        put_conn(ctx, conn)


@mcp.tool()
def build_hybrid_playlist(
    ctx: Context,
    duration_minutes: int = 45,
    difficulty: str | None = None,
    theme: str | None = None,
    audience: str | None = None,
    target_tracks: int | None = None,
) -> str:
    """Build a full playlist using DB anchors plus OpenAI gap-fill suggestions.

    This tool:
    1) Builds a base playlist from local DB tracks.
    2) Applies feedback filters to remove disliked tracks/artists.
    3) Uses OpenAI to suggest additional tracks only when DB coverage is short.
    4) Returns a merged, ordered playlist.
    """
    base_raw = build_class_playlist(
        ctx,
        duration_minutes=duration_minutes,
        difficulty=difficulty,
        theme=theme,
        audience=audience,
    )
    base = json.loads(base_raw)
    playlist = base.get("playlist", []) if isinstance(base, dict) else []

    conn = get_conn(ctx)
    try:
        feedback = fetch_feedback_signals(conn, audience=audience)
    finally:
        put_conn(ctx, conn)

    disliked_titles = {t.lower().strip() for t in feedback.get("disliked_titles", [])}
    disliked_artists = {a.lower().strip() for a in feedback.get("disliked_artists", [])}

    def allowed(track: dict[str, Any]) -> bool:
        title = str(track.get("title") or "").lower().strip()
        artist = str(track.get("artist") or "").lower().strip()
        if not title or not artist:
            return False
        if title in disliked_titles or artist in disliked_artists:
            return False
        return True

    db_tracks_flat: list[dict[str, Any]] = []
    seen: set[str] = set()
    for phase in playlist:
        tracks = phase.get("tracks", []) or []
        filtered: list[dict[str, Any]] = []
        for track in tracks:
            if not isinstance(track, dict) or not allowed(track):
                continue
            key = normalize_track_key(track.get("title"), track.get("artist"))
            if key in seen:
                continue
            seen.add(key)
            t = dict(track)
            t["source"] = "db"
            filtered.append(t)
            db_tracks_flat.append(
                {
                    "title": t.get("title"),
                    "artist": t.get("artist"),
                    "bpm": t.get("bpm"),
                    "track_type": t.get("track_type"),
                    "phase": phase.get("phase"),
                }
            )
        phase["tracks"] = filtered

    desired_count = derive_target_track_count(duration_minutes, target_tracks)
    needed_count = max(0, desired_count - len(db_tracks_flat))

    ai_tracks = suggest_external_tracks_with_openai(
        duration_minutes=duration_minutes,
        difficulty=difficulty,
        theme=theme,
        audience=audience,
        needed_count=needed_count,
        existing_tracks=db_tracks_flat,
        feedback_signals=feedback,
    )

    added_ai = 0
    for item in ai_tracks:
        if added_ai >= needed_count:
            break
        title = item.get("title")
        artist = item.get("artist")
        key = normalize_track_key(title, artist)
        if key in seen:
            continue
        if str(title).lower().strip() in disliked_titles:
            continue
        if str(artist).lower().strip() in disliked_artists:
            continue
        seen.add(key)
        track = {
            "id": None,
            "spotify_id": None,
            "title": title,
            "artist": artist,
            "bpm": item.get("estimated_bpm"),
            "intensity": "medium",
            "track_type": item.get("focus_area"),
            "duration_minutes": None,
            "position": None,
            "focus_area": item.get("focus_area"),
            "resistance_min": None,
            "resistance_max": None,
            "cadence_min": None,
            "cadence_max": None,
            "spotify_url": None,
            "thumbs_up": 0,
            "thumbs_down": 0,
            "notes": item.get("notes"),
            "source": "ai",
        }
        add_track_to_phase(
            playlist, focus_area_to_phase_name(str(item.get("focus_area") or "")), track
        )
        added_ai += 1

    tracks_flat: list[dict[str, Any]] = []
    for phase in playlist:
        for track in phase.get("tracks", []) or []:
            t = dict(track)
            t["phase"] = phase.get("phase")
            tracks_flat.append(t)

    total_duration = sum((t.get("duration_minutes") or 0) for t in tracks_flat)
    result = {
        "target_duration": duration_minutes,
        "estimated_duration": round(total_duration, 1),
        "difficulty": difficulty,
        "theme": theme,
        "target_track_count": desired_count,
        "db_track_count": len(db_tracks_flat),
        "ai_track_count": added_ai,
        "total_tracks": len(tracks_flat),
        "feedback_signals": feedback,
        "playlist": playlist,
        "tracks": tracks_flat,
    }
    return json.dumps(result, indent=2)


@mcp.tool()
def recommend_class_tracks(
    ctx: Context,
    class_length_minutes: int = 55,
    theme: str | None = None,
    vibe: str | None = None,
    custom_intensity_arc: str | None = None,
    preferred_genres: str | None = None,
    preferred_artists: str | None = None,
    exclude_genres: str | None = None,
    exclude_songs_or_artists: str | None = None,
    audience: str | None = None,
) -> str:
    """Return OpenAI-suggested tracks, enriched from DB when available.

    Inputs mirror UI fields:
    - Class length
    - Theme / vibe
    - Custom intensity arc (comma-separated, optional)
    - Preferred genres/artists (comma-separated, optional)
    - Excluded genres and songs/artists (comma-separated, optional)

    Output shape:
    - If a suggested track exists in local DB (title+artist), return full DB track schema + suggest_type.
    - Otherwise return: title, artist, bpm, suggest_type.
    """
    conn = get_conn(ctx)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        preferred_genre_list = parse_csv_list(preferred_genres)
        preferred_artist_list = parse_csv_list(preferred_artists)
        excluded_genre_list = [g.lower() for g in parse_csv_list(exclude_genres)]
        excluded_song_artist_list = parse_csv_list(exclude_songs_or_artists)
        arc = normalize_arc_types(custom_intensity_arc, class_length_minutes)

        # Feedback signals for ranking and filtering.
        feedback = fetch_feedback_signals(conn, audience=audience)
        disliked_titles = {
            t.lower().strip() for t in feedback.get("disliked_titles", [])
        }
        disliked_artists = {
            a.lower().strip() for a in feedback.get("disliked_artists", [])
        }

        # Track count target (about 3.5-4.5 minutes average per song).
        target_count = max(8, min(20, round(class_length_minutes / 4)))

        theme_parts = []
        if theme:
            theme_parts.append(f"Theme: {theme}")
        if vibe:
            theme_parts.append(f"Vibe: {vibe}")
        if arc:
            theme_parts.append(f"Class intensity arc: {', '.join(arc)}")
        if preferred_genre_list:
            theme_parts.append(f"Preferred genres: {', '.join(preferred_genre_list)}")
        if preferred_artist_list:
            theme_parts.append(f"Preferred artists: {', '.join(preferred_artist_list)}")
        if excluded_genre_list:
            theme_parts.append(f"Excluded genres: {', '.join(excluded_genre_list)}")
        if excluded_song_artist_list:
            theme_parts.append(
                f"Excluded songs or artists: {', '.join(excluded_song_artist_list)}"
            )

        composed_theme = " | ".join(theme_parts) if theme_parts else None

        # OpenAI-first flow: generate candidate tracks using feedback + preferences.
        ai_tracks = suggest_external_tracks_with_openai(
            duration_minutes=class_length_minutes,
            difficulty=None,
            theme=composed_theme,
            audience=audience,
            needed_count=target_count,
            existing_tracks=[],
            feedback_signals=feedback,
        )

        used: set[str] = set()
        results: list[dict[str, Any]] = []
        excluded_tokens = [x.lower() for x in excluded_song_artist_list]
        for t in ai_tracks:
            if len(results) >= target_count:
                break

            title = str(t.get("title") or "").strip()
            artist = str(t.get("artist") or "").strip()
            if not title or not artist:
                continue

            title_lower = title.lower()
            artist_lower = artist.lower()
            key = normalize_track_key(title, artist)
            if key in used:
                continue
            if title_lower in disliked_titles or artist_lower in disliked_artists:
                continue
            if any(
                token and (token in title_lower or token in artist_lower)
                for token in excluded_tokens
            ):
                continue

            suggest_type = str(t.get("focus_area") or "build").lower()
            if suggest_type not in {
                "warmup",
                "build",
                "climb",
                "sprint",
                "recovery",
                "cooldown",
            }:
                suggest_type = "build"

            cur.execute(
                """
                SELECT *
                FROM tracks
                WHERE LOWER(TRIM(title)) = LOWER(TRIM(%s))
                  AND LOWER(TRIM(artist)) = LOWER(TRIM(%s))
                LIMIT 1
                """,
                (title, artist),
            )
            existing = cur.fetchone()

            used.add(key)
            if existing:
                # If the AI suggestion exists in DB, return full DB track schema.
                full_track = {k: serialize(v) for k, v in dict(existing).items()}
                full_track["suggest_type"] = suggest_type
                results.append(full_track)
            else:
                # If not in DB, return minimal shape only.
                results.append(
                    {
                        "title": title,
                        "artist": artist,
                        "bpm": t.get("estimated_bpm"),
                        "suggest_type": suggest_type,
                    }
                )

        return json.dumps({"tracks": results}, indent=2)
    finally:
        put_conn(ctx, conn)


@mcp.tool()
def list_routines(
    ctx: Context,
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

        cur.execute(
            f"""
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
        """,
            params,
        )

        rows = serialize_rows(cur.fetchall())
        cur.close()
        return json.dumps(rows, indent=2)
    finally:
        put_conn(ctx, conn)


@mcp.tool()
def rate_track(
    ctx: Context,
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
    if rating not in ("up", "down"):
        return json.dumps({"error": "Rating must be 'up' or 'down'"})

    conn = get_conn(ctx)
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        # Look up the track to get artist and spotify_id
        cur.execute(
            "SELECT title, artist, spotify_id FROM tracks WHERE title ILIKE %s LIMIT 1",
            (f"%{track_title}%",),
        )
        track = cur.fetchone()

        if not track:
            cur.close()
            return json.dumps({"error": f"Track '{track_title}' not found in database"})

        track_title_exact = track["title"]
        track_artist = track["artist"]
        spotify_id = track["spotify_id"]

        # Insert into local database
        cur.execute(
            """
            INSERT INTO track_feedback (
                track_title, track_artist, spotify_id,
                rating, context, audience, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (track_title, rating, COALESCE(context, ''), COALESCE(audience, ''))
            DO UPDATE SET updated_at = CURRENT_TIMESTAMP
            RETURNING id
        """,
            (track_title_exact, track_artist, spotify_id, rating, context, audience),
        )

        feedback_id = cur.fetchone()["id"]
        conn.commit()

        # Sync to base44
        app_ctx = ctx.request_context.lifespan_context
        base44_result = None
        if app_ctx.base44_api_key and app_ctx.base44_app_id:
            try:
                response = requests.post(
                    f"{app_ctx.base44_api_url}/apps/{app_ctx.base44_app_id}/entities/TrackFeedback",
                    headers={
                        "api_key": app_ctx.base44_api_key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "track_title": track_title_exact,
                        "track_artist": track_artist,
                        "spotify_id": spotify_id,
                        "rating": rating,
                        "context": context or "",
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
        return json.dumps(
            {
                "status": "saved",
                "feedback_id": feedback_id,
                "track": track_title_exact,
                "artist": track_artist,
                "rating": f"{emoji} {rating}",
                "context": context,
                "audience": audience,
                "base44_sync": base44_result or "skipped (no API credentials)",
            },
            indent=2,
        )
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
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("DB_NAME", "choreography"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)

        cur.execute("SELECT COUNT(*) as total FROM tracks")
        total = cur.fetchone()["total"]

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
        return json.dumps(
            {
                "total_tracks": total,
                "by_track_type": by_type,
                "by_intensity": by_intensity,
                "bpm_range": bpm_range,
            },
            indent=2,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


@mcp.prompt()
def build_class(
    duration: str = "45", difficulty: str = "intermediate", audience: str = "50+"
) -> str:
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
    parser = argparse.ArgumentParser(description="Run choreography MCP server.")
    parser.add_argument(
        "--transport",
        default=os.getenv("MCP_TRANSPORT", "stdio"),
        help="MCP transport (default: stdio).",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("MCP_HOST", "127.0.0.1"),
        help="Host for SSE/HTTP transports (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MCP_PORT", "8000")),
        help="Port for SSE/HTTP transports (default: 8000).",
    )
    parser.add_argument(
        "--mount-path",
        default=os.getenv("MCP_MOUNT_PATH", "/"),
        help="Mount path for SSE transport app (default: /).",
    )
    parser.add_argument(
        "--sse-path",
        default=os.getenv("MCP_SSE_PATH", "/sse"),
        help="SSE endpoint path (default: /sse).",
    )
    parser.add_argument(
        "--message-path",
        default=os.getenv("MCP_MESSAGE_PATH", "/messages/"),
        help="SSE message endpoint path (default: /messages/).",
    )
    parser.add_argument(
        "--http-path",
        default=os.getenv("MCP_HTTP_PATH", "/mcp"),
        help="Streamable HTTP endpoint path (default: /mcp).",
    )
    args = parser.parse_args()

    # Apply network settings before transport startup.
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.settings.mount_path = args.mount_path
    mcp.settings.sse_path = args.sse_path
    mcp.settings.message_path = args.message_path
    mcp.settings.streamable_http_path = args.http_path

    logger.info(
        "Starting MCP server transport=%s host=%s port=%s",
        args.transport,
        mcp.settings.host,
        mcp.settings.port,
    )
    mcp.run(transport=args.transport, mount_path=args.mount_path)
