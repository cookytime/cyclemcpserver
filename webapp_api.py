#!/usr/bin/env python3
"""
Web API that combines MCP playlist generation with OpenAI curation.

Security:
- Requires X-API-Key header on protected routes.

Dependencies:
- fastapi
- uvicorn
- requests
- mcp
"""

import json
import os
import re
from typing import Any

import httpx
import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, Field

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))


class PlaylistRequest(BaseModel):
    duration_minutes: int = Field(default=45, ge=20, le=120)
    difficulty: str | None = Field(default="intermediate")
    audience: str | None = Field(default="mixed")
    theme: str | None = Field(default=None)
    intensity_arc: str | None = Field(default="Build -> Peak -> Recover -> Finish")
    vibe: str | None = Field(default="high energy")
    preferred_genres: list[str] = Field(default_factory=list)
    preferred_artists: list[str] = Field(default_factory=list)
    excluded_genres: list[str] = Field(default_factory=list)
    spotify_access_token: str | None = Field(default=None)
    user_goal: str | None = Field(
        default="Build a fun class flow with smooth transitions and strong energy arc."
    )
    debug: bool = Field(default=False)


class RoutinePayload(BaseModel):
    name: str
    description: str = ""
    theme: str = ""
    intensity_arc: str = ""
    resistance_scale_notes: str = ""
    class_summary: str = ""
    total_duration_minutes: float = 0.0
    difficulty: str = "intermediate"
    track_ids: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    spotify_playlist_id: str = ""


class FeedbackSignals(BaseModel):
    liked_titles: list[str] = Field(default_factory=list)
    liked_artists: list[str] = Field(default_factory=list)
    disliked_titles: list[str] = Field(default_factory=list)
    disliked_artists: list[str] = Field(default_factory=list)


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = os.getenv("WEBAPP_API_KEY", "")
    if not expected:
        raise HTTPException(
            status_code=500,
            detail="Server is missing WEBAPP_API_KEY configuration.",
        )
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid API key.")


def extract_mcp_text(result: Any) -> str:
    chunks: list[str] = []
    for block in getattr(result, "content", []) or []:
        if getattr(block, "type", None) == "text" and getattr(block, "text", None):
            chunks.append(block.text)
    return "\n".join(chunks).strip()


def extract_mcp_error(result: Any) -> str:
    text = extract_mcp_text(result)
    if text:
        return text
    return "MCP returned an error without message."


def flatten_exception_messages(exc: BaseException) -> list[str]:
    if isinstance(exc, BaseExceptionGroup):
        messages: list[str] = []
        for inner in exc.exceptions:
            messages.extend(flatten_exception_messages(inner))
        return messages

    msg = str(exc).strip()
    if not msg:
        msg = repr(exc)
    return [f"{type(exc).__name__}: {msg}"]


async def call_mcp_tool(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
) -> Any:
    mcp_url = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8000/mcp")
    mcp_bearer = os.getenv("MCP_SERVER_BEARER_TOKEN", "").strip()

    try:
        if mcp_bearer:
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {mcp_bearer}"}
            ) as http_client:
                async with streamable_http_client(mcp_url, http_client=http_client) as (
                    read_stream,
                    write_stream,
                    _,
                ):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        result = await session.call_tool(
                            name=tool_name,
                            arguments=arguments or {},
                        )
                        if result.isError:
                            raise RuntimeError(
                                f"MCP tool {tool_name} failed: {extract_mcp_error(result)}"
                            )
                        text = extract_mcp_text(result)
                        if not text:
                            return {}
                        return json.loads(text)
        else:
            async with streamable_http_client(mcp_url) as (
                read_stream,
                write_stream,
                _,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        name=tool_name,
                        arguments=arguments or {},
                    )
                    if result.isError:
                        raise RuntimeError(
                            f"MCP tool {tool_name} failed: {extract_mcp_error(result)}"
                        )
                    text = extract_mcp_text(result)
                    if not text:
                        return {}
                    return json.loads(text)
    except Exception as e:
        leaves = "; ".join(flatten_exception_messages(e))
        raise RuntimeError(f"Failed calling MCP at {mcp_url}: {leaves}") from e


async def call_mcp_resource(uri: str) -> Any:
    mcp_url = os.getenv("MCP_SERVER_URL", "http://127.0.0.1:8000/mcp")
    mcp_bearer = os.getenv("MCP_SERVER_BEARER_TOKEN", "").strip()

    try:
        if mcp_bearer:
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {mcp_bearer}"}
            ) as http_client:
                async with streamable_http_client(mcp_url, http_client=http_client) as (
                    read_stream,
                    write_stream,
                    _,
                ):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        result = await session.read_resource(uri)
                        chunks: list[str] = []
                        for block in getattr(result, "contents", []) or []:
                            text = getattr(block, "text", None)
                            if text:
                                chunks.append(text)
                        joined = "\n".join(chunks).strip()
                        if not joined:
                            return {}
                        return json.loads(joined)
        else:
            async with streamable_http_client(mcp_url) as (
                read_stream,
                write_stream,
                _,
            ):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.read_resource(uri)
                    chunks: list[str] = []
                    for block in getattr(result, "contents", []) or []:
                        text = getattr(block, "text", None)
                        if text:
                            chunks.append(text)
                    joined = "\n".join(chunks).strip()
                    if not joined:
                        return {}
                    return json.loads(joined)
    except Exception as e:
        leaves = "; ".join(flatten_exception_messages(e))
        raise RuntimeError(f"Failed reading MCP resource at {mcp_url}: {leaves}") from e


def call_openai_playlist_curation(
    request_data: PlaylistRequest,
    stats: dict[str, Any],
    playlist: dict[str, Any],
    feedback: FeedbackSignals,
) -> dict[str, Any]:
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return {
            "status": "skipped",
            "reason": "OPENAI_API_KEY not configured",
            "curated_playlist": None,
        }

    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    timeout = int(os.getenv("OPENAI_TIMEOUT_SECONDS", "45"))

    system_prompt = """You are an expert cycling class music programmer and DJ.

Your job is to curate a structured Spotify-ready track list
for a cycling class based on the provided inputs.

You must:
- Use the database-provided track suggestions as primary anchors.
- Add non-database tracks only to fill gaps or enrich transitions.
- Select tracks that are available on Spotify.
- Prioritize high-energy, rhythm-driven songs suitable for indoor cycling.
- Match the requested intensity arc.
- Respect preferred and excluded genres.
- Favor preferred artists when appropriate.
- Avoid artists or genres listed in exclusions.
- Avoid any tracks/artists listed as disliked feedback.
- Prefer tracks/artists listed as liked feedback when they fit the arc.
- Ensure BPM suitability for cycling
  (generally 80–100 for climbs, 100–130+ for intervals unless stylistically justified).
- Create a cohesive emotional and energy journey.

Output Requirements:
- Return 10–15 tracks.
- Provide:
  - Title
  - Artist
  - Estimated BPM
  - Energy Level (1–10)
  - Suggested Ride Segment (Warmup, Build, Climb, Sprint, Recovery, Cooldown)
  - 1 short sentence explaining why it fits

Rules:
- Avoid repeating artists unless specifically requested.
- Avoid deep cuts unless they strongly match the theme.
- Prefer recognizable but not overplayed tracks.
- Match vibe and theme before pure popularity.
- If preferred artists conflict with excluded genres, respect exclusions.
- If the theme implies a strong emotional tone
  (e.g., empowerment, revenge, nostalgia), lean into lyrical content.
- Do not explain your reasoning outside the structured list.

Return valid JSON only."""

    preferred_genres = (
        ", ".join(request_data.preferred_genres)
        if request_data.preferred_genres
        else "any"
    )
    preferred_artists = (
        ", ".join(request_data.preferred_artists)
        if request_data.preferred_artists
        else "any"
    )
    excluded_genres = (
        ", ".join(request_data.excluded_genres)
        if request_data.excluded_genres
        else "none"
    )

    user_prompt = f"""Create a playlist of songs for a
{request_data.duration_minutes}-minute cycling class.
Theme: {request_data.theme or "any"}
Intensity arc: {request_data.intensity_arc or "any"}
Vibe: {request_data.vibe or "any"}
Preferred genres: {preferred_genres}
Preferred artists: {preferred_artists}
Excluded genres: {excluded_genres}

Use the MCP suggestions and metadata as your source list.
Return a JSON array of tracks with the following fields:
- title (string)
- artist (string)
- estimated_bpm (number)
- energy_level (number 1-10)
- focus_area (string: warmup, build, climb, sprint, recovery, cooldown)
- notes (string)
Ensure variety, keep pacing aligned with intensity arc, and match the vibe/theme."""

    context_payload = {
        "request": request_data.model_dump(),
        "track_stats": stats,
        "mcp_playlist_suggestions": playlist,
        "feedback_signals": feedback.model_dump(),
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
                {"role": "user", "content": user_prompt},
                {
                    "role": "user",
                    "content": f"MCP data context: {json.dumps(context_payload)}",
                },
            ],
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    content = payload["choices"][0]["message"]["content"]
    parsed = json.loads(content)
    curated_tracks: list[dict[str, Any]]
    if isinstance(parsed, list):
        curated_tracks = parsed
    elif isinstance(parsed, dict) and isinstance(parsed.get("tracks"), list):
        curated_tracks = parsed["tracks"]
    else:
        curated_tracks = []

    return {"status": "ok", "model": model, "curated_playlist": curated_tracks}


def normalize_slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def build_track_id(track: dict[str, Any]) -> str:
    if track.get("spotify_id"):
        return f"spotify:{track['spotify_id']}"
    if track.get("base44_id"):
        return f"base44:{track['base44_id']}"
    if track.get("id") is not None:
        return f"db:{track['id']}"

    title = str(track.get("title") or "unknown-title")
    artist = str(track.get("artist") or "unknown-artist")
    return f"fallback:{normalize_slug(title)}:{normalize_slug(artist)}"


def build_raw_track_list(
    openai_result: dict[str, Any],
    mcp_playlist: dict[str, Any],
    feedback: FeedbackSignals,
    target_count: int,
) -> list[dict[str, Any]]:
    disliked_titles = {t.lower().strip() for t in feedback.disliked_titles}
    disliked_artists = {a.lower().strip() for a in feedback.disliked_artists}

    def allowed(title: str, artist: str) -> bool:
        return (
            title.lower().strip() not in disliked_titles
            and artist.lower().strip() not in disliked_artists
        )

    # Base set from DB (anchor tracks).
    db_tracks: list[dict[str, Any]] = []
    for phase in mcp_playlist.get("playlist", []) or []:
        phase_name = str(phase.get("phase") or "warmup").lower()
        for track in phase.get("tracks", []) or []:
            if not isinstance(track, dict):
                continue
            title = str(track.get("title") or "")
            artist = str(track.get("artist") or "")
            if not title or not artist or not allowed(title, artist):
                continue
            db_tracks.append(
                {
                    "title": title,
                    "artist": artist,
                    "estimated_bpm": track.get("bpm"),
                    "focus_area": phase_name,
                    "notes": str(track.get("notes") or ""),
                }
            )

    # Add OpenAI enrichment tracks only as gap-fill.
    curated = (
        openai_result.get("curated_playlist")
        if isinstance(openai_result, dict)
        else None
    )
    ai_tracks: list[dict[str, Any]] = []
    if isinstance(curated, list):
        for item in curated:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "")
            artist = str(item.get("artist") or "")
            if not title or not artist or not allowed(title, artist):
                continue
            ai_tracks.append(
                {
                    "title": title,
                    "artist": artist,
                    "estimated_bpm": item.get("estimated_bpm"),
                    "focus_area": str(item.get("focus_area") or "warmup").lower(),
                    "notes": str(item.get("notes") or ""),
                }
            )

    merged: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_unique(track: dict[str, Any]) -> None:
        key = f"{track['title'].lower().strip()}|{track['artist'].lower().strip()}"
        if key in seen:
            return
        seen.add(key)
        merged.append(track)

    for t in db_tracks:
        add_unique(t)
    for t in ai_tracks:
        if len(merged) >= target_count:
            break
        add_unique(t)

    return merged[:target_count]


def estimate_target_track_count(duration_minutes: int) -> int:
    if duration_minutes <= 30:
        return 10
    if duration_minutes <= 45:
        return 12
    return 15


def parse_feedback_signals(up_rows: Any, down_rows: Any) -> FeedbackSignals:
    liked_titles: set[str] = set()
    liked_artists: set[str] = set()
    disliked_titles: set[str] = set()
    disliked_artists: set[str] = set()

    if isinstance(up_rows, list):
        for row in up_rows:
            if not isinstance(row, dict):
                continue
            t = str(row.get("track_title") or "").strip()
            a = str(row.get("track_artist") or "").strip()
            if t:
                liked_titles.add(t)
            if a:
                liked_artists.add(a)

    if isinstance(down_rows, list):
        for row in down_rows:
            if not isinstance(row, dict):
                continue
            t = str(row.get("track_title") or "").strip()
            a = str(row.get("track_artist") or "").strip()
            if t:
                disliked_titles.add(t)
            if a:
                disliked_artists.add(a)

    return FeedbackSignals(
        liked_titles=sorted(liked_titles),
        liked_artists=sorted(liked_artists),
        disliked_titles=sorted(disliked_titles),
        disliked_artists=sorted(disliked_artists),
    )


def spotify_search_first_track(
    access_token: str, title: str, artist: str
) -> dict[str, Any] | None:
    resp = requests.get(
        "https://api.spotify.com/v1/search",
        params={"q": f"{title} {artist}", "type": "track", "limit": 1},
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15,
    )
    if not resp.ok:
        return None
    data = resp.json()
    items = data.get("tracks", {}).get("items", [])
    if not items:
        return None
    return items[0]


def enrich_tracks_with_spotify(
    raw_tracks: list[dict[str, Any]],
    access_token: str | None,
) -> list[dict[str, Any]]:
    if not access_token:
        return raw_tracks

    matched: list[dict[str, Any]] = []
    for track in raw_tracks:
        hit = spotify_search_first_track(access_token, track["title"], track["artist"])
        if not hit:
            continue
        matched.append(
            {
                "title": hit.get("name", ""),
                "artist": (hit.get("artists") or [{}])[0].get("name", ""),
                "album": (hit.get("album") or {}).get("name", ""),
                "spotify_id": hit.get("id", ""),
                "spotify_album_art": ((hit.get("album") or {}).get("images") or [{}])[
                    0
                ].get("url"),
                "spotify_url": (hit.get("external_urls") or {}).get("spotify", ""),
                "duration_minutes": (hit.get("duration_ms") or 0) / 60000,
                "bpm": track.get("estimated_bpm"),
                "track_type": track.get("focus_area") or "warmup",
                "notes": track.get("notes") or "",
            }
        )
    return matched


def flatten_playlist_tracks(mcp_playlist: dict[str, Any]) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    for phase in mcp_playlist.get("playlist", []) or []:
        phase_name = phase.get("phase")
        for track in phase.get("tracks", []) or []:
            t = dict(track)
            t["phase"] = phase_name
            ordered.append(t)
    return ordered


def build_routine_payload(
    request_data: PlaylistRequest,
    mcp_playlist: dict[str, Any],
    openai_result: dict[str, Any],
) -> RoutinePayload:
    flattened_tracks = flatten_playlist_tracks(mcp_playlist)
    track_lookup: dict[str, dict[str, Any]] = {}
    for track in flattened_tracks:
        key = f"{str(track.get('title', '')).lower()}|{str(track.get('artist', '')).lower()}"
        track_lookup[key] = track

    ordered_tracks = flattened_tracks
    curated = (
        openai_result.get("curated_playlist")
        if isinstance(openai_result, dict)
        else None
    )
    curated_tracks = curated if isinstance(curated, list) else None
    if isinstance(curated_tracks, list) and curated_tracks:
        selected: list[dict[str, Any]] = []
        for item in curated_tracks:
            if not isinstance(item, dict):
                continue
            key = f"{str(item.get('title', '')).lower()}|{str(item.get('artist', '')).lower()}"
            track = track_lookup.get(key)
            if track:
                selected.append(track)
        if selected:
            ordered_tracks = selected

    track_ids = [build_track_id(track) for track in ordered_tracks]

    requested_difficulty = (request_data.difficulty or "").lower()
    difficulty = (
        requested_difficulty
        if requested_difficulty in {"beginner", "intermediate", "advanced", "expert"}
        else "intermediate"
    )
    theme = request_data.theme or "Mixed energy ride"
    duration = mcp_playlist.get("estimated_duration") or request_data.duration_minutes
    class_summary = (
        f"{len(track_ids)} tracks selected for a {request_data.duration_minutes}-minute target "
        f"({duration} minutes estimated). Audience: {request_data.audience or 'mixed'}."
    )
    tags = [
        tag
        for tag in [
            "ai-generated",
            difficulty or "",
            request_data.audience or "",
            request_data.theme or "",
        ]
        if tag
    ]

    return RoutinePayload(
        name=f"{request_data.duration_minutes}-min {difficulty} ride",
        description=request_data.user_goal or "",
        theme=theme,
        intensity_arc="Warmup -> Build -> Peak -> Recovery -> Finish",
        resistance_scale_notes="1 = flat road, 10 = max hill",
        class_summary=class_summary,
        total_duration_minutes=float(duration),
        difficulty=difficulty,
        track_ids=track_ids,
        tags=tags,
        spotify_playlist_id="",
    )


app = FastAPI(title="Cycle MCP Server Web API", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/playlist")
async def generate_playlist(
    request_data: PlaylistRequest,
    _auth: None = Depends(require_api_key),
) -> dict[str, Any]:
    try:
        stats = await call_mcp_resource("stats://tracks")
    except Exception:
        stats = {}
    try:
        up_rows = await call_mcp_tool(
            "get_top_rated_tracks",
            {"rating": "up", "audience": request_data.audience, "limit": 50},
        )
    except Exception:
        up_rows = []
    try:
        down_rows = await call_mcp_tool(
            "get_top_rated_tracks",
            {"rating": "down", "audience": request_data.audience, "limit": 50},
        )
    except Exception:
        down_rows = []
    feedback = parse_feedback_signals(up_rows, down_rows)

    try:
        playlist = await call_mcp_tool(
            "build_class_playlist",
            {
                "duration_minutes": request_data.duration_minutes,
                "difficulty": request_data.difficulty,
                "theme": request_data.theme,
                "audience": request_data.audience,
            },
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"MCP build failed: {e}") from e

    openai_result: dict[str, Any]
    try:
        openai_result = call_openai_playlist_curation(
            request_data, stats, playlist, feedback
        )
    except Exception as e:
        openai_result = {
            "status": "failed",
            "reason": str(e),
            "curated_playlist": None,
        }
    routine = build_routine_payload(request_data, playlist, openai_result)

    routine_payload = routine.model_dump()
    if request_data.debug:
        return {
            "routine": routine_payload,
            "debug": {
                "request": request_data.model_dump(),
                "mcp_track_stats": stats,
                "mcp_playlist": playlist,
                "feedback_signals": feedback.model_dump(),
                "openai": openai_result,
            },
        }
    return routine_payload


@app.post("/api/tracks")
async def generate_tracks(
    request_data: PlaylistRequest,
    _auth: None = Depends(require_api_key),
) -> dict[str, Any]:
    try:
        stats = await call_mcp_resource("stats://tracks")
    except Exception:
        stats = {}
    try:
        up_rows = await call_mcp_tool(
            "get_top_rated_tracks",
            {"rating": "up", "audience": request_data.audience, "limit": 50},
        )
    except Exception:
        up_rows = []
    try:
        down_rows = await call_mcp_tool(
            "get_top_rated_tracks",
            {"rating": "down", "audience": request_data.audience, "limit": 50},
        )
    except Exception:
        down_rows = []
    feedback = parse_feedback_signals(up_rows, down_rows)

    try:
        playlist = await call_mcp_tool(
            "build_class_playlist",
            {
                "duration_minutes": request_data.duration_minutes,
                "difficulty": request_data.difficulty,
                "theme": request_data.theme,
                "audience": request_data.audience,
            },
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"MCP build failed: {e}") from e

    try:
        openai_result = call_openai_playlist_curation(
            request_data, stats, playlist, feedback
        )
    except Exception as e:
        raise HTTPException(
            status_code=502, detail=f"OpenAI curation failed: {e}"
        ) from e

    raw_tracks = build_raw_track_list(
        openai_result,
        playlist,
        feedback,
        estimate_target_track_count(request_data.duration_minutes),
    )
    tracks = enrich_tracks_with_spotify(raw_tracks, request_data.spotify_access_token)

    if request_data.debug:
        return {
            "tracks": tracks,
            "debug": {
                "request": request_data.model_dump(),
                "mcp_track_stats": stats,
                "mcp_playlist": playlist,
                "feedback_signals": feedback.model_dump(),
                "openai": openai_result,
                "raw_tracks_before_spotify": raw_tracks,
            },
        }
    return {"tracks": tracks}
