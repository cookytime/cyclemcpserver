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
import time
import hmac
import base64
import hashlib
import asyncio
import logging
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Any
from pathlib import Path

import httpx
import psycopg2
import requests
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel, Field
from psycopg2.extras import Json
from pydantic import AnyUrl, BaseModel, Field

from config import Config

from config import Config

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
logger = logging.getLogger("cycle-webapi")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEBHOOK_MAX_SKEW_SECONDS = int(os.getenv("WEBHOOK_MAX_SKEW_SECONDS", "300"))
WEBHOOK_STATE_FILE = Path(
    os.getenv("WEBHOOK_STATE_FILE", ".run/webhook_state.json")
).expanduser()
WEBHOOK_MAX_EVENT_IDS = int(os.getenv("WEBHOOK_MAX_EVENT_IDS", "5000"))


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


class ChoreographyWebhookEvent(BaseModel):
    event: str
    choreography_id: str
    version: int
    updated_at: str
    mode: str = "notify"
    payload: dict[str, Any] | None = None
    source: str | None = None


class RoutineWebhookEvent(BaseModel):
    event: str
    routine_id: str
    version: int
    updated_at: str
    mode: str = "notify"
    payload: dict[str, Any] | None = None
    source: str | None = None


class WebhookStateStore:
    def __init__(self, path: Path, max_event_ids: int = 5000):
        self.path = path
        self.max_event_ids = max_event_ids
        self._lock = asyncio.Lock()
        self._event_ids_order: list[str] = []
        self._event_ids_seen: set[str] = set()
        self._latest_versions: dict[str, int] = {}
        self._loaded = False

    def _load_locked(self) -> None:
        if self._loaded:
            return

        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                raw = {}
        else:
            raw = {}

        ids = raw.get("event_ids") if isinstance(raw, dict) else []
        versions = raw.get("latest_versions") if isinstance(raw, dict) else {}

        if isinstance(ids, list):
            clean_ids = [str(item) for item in ids if isinstance(item, str)]
            self._event_ids_order = clean_ids[-self.max_event_ids :]
            self._event_ids_seen = set(self._event_ids_order)

        if isinstance(versions, dict):
            normalized: dict[str, int] = {}
            for key, value in versions.items():
                try:
                    normalized[str(key)] = int(value)
                except Exception:
                    continue
            self._latest_versions = normalized

        self._loaded = True

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "event_ids": self._event_ids_order[-self.max_event_ids :],
            "latest_versions": self._latest_versions,
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    async def classify_and_mark(
        self, event_id: str, choreography_id: str, version: int
    ) -> str:
        async with self._lock:
            self._load_locked()

            if event_id in self._event_ids_seen:
                return "duplicate"

            latest = self._latest_versions.get(choreography_id)
            if latest is not None and version <= latest:
                return "stale"

            self._event_ids_order.append(event_id)
            self._event_ids_seen.add(event_id)
            if len(self._event_ids_order) > self.max_event_ids:
                drop = self._event_ids_order.pop(0)
                self._event_ids_seen.discard(drop)

            self._latest_versions[choreography_id] = version
            self._save_locked()
            return "accepted"


webhook_state_store = WebhookStateStore(WEBHOOK_STATE_FILE, WEBHOOK_MAX_EVENT_IDS)
webhook_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
webhook_worker_task: asyncio.Task[None] | None = None


def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
    expected = os.getenv("WEBAPP_API_KEY", "")
    if not expected:
        raise HTTPException(
            status_code=500,
            detail="Server is missing WEBAPP_API_KEY configuration.",
        )
    if x_api_key != expected:
        raise HTTPException(status_code=401, detail="Invalid API key.")


def require_webhook_signature(
    request: Request,
    raw_body: bytes,
    x_webhook_timestamp: str | None,
    x_webhook_signature: str | None,
) -> None:
    if not WEBHOOK_SECRET:
        raise HTTPException(
            status_code=500,
            detail="Server is missing WEBHOOK_SECRET configuration.",
        )

    if not x_webhook_timestamp or not x_webhook_signature:
        raise HTTPException(status_code=401, detail="Missing webhook signature headers.")

    try:
        timestamp = int(x_webhook_timestamp)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid webhook timestamp header.") from exc

    now = int(time.time())
    if abs(now - timestamp) > WEBHOOK_MAX_SKEW_SECONDS:
        raise HTTPException(status_code=401, detail="Webhook timestamp outside allowed window.")

    signed_payload = f"{x_webhook_timestamp}.".encode("utf-8") + raw_body
    expected_hex = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()
    expected_b64 = hmac.new(
        WEBHOOK_SECRET.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).digest()
    expected_b64_text = base64.b64encode(expected_b64).decode("utf-8")

    raw_sig = x_webhook_signature.strip()
    candidates: list[str] = []
    if "=" in raw_sig:
        _, rhs = raw_sig.split("=", 1)
        candidates.append(rhs.strip())
    if ":" in raw_sig:
        _, rhs = raw_sig.split(":", 1)
        candidates.append(rhs.strip())
    candidates.append(raw_sig)

    # Deduplicate while preserving order.
    seen: set[str] = set()
    supplied_values = [s for s in candidates if s and not (s in seen or seen.add(s))]

    if not any(
        hmac.compare_digest(expected_hex, supplied)
        or hmac.compare_digest(expected_b64_text, supplied)
        for supplied in supplied_values
    ):
        client = request.client.host if request.client else "unknown"
        logger.warning("Rejected webhook with bad signature from %s", client)
        raise HTTPException(status_code=401, detail="Invalid webhook signature.")


def _coerce_version(value: Any, updated_at: str | None) -> int:
    if value is not None:
        try:
            return int(value)
        except Exception:
            pass
    if updated_at:
        try:
            parsed = updated_at.replace("Z", "+00:00")
            return int(datetime.fromisoformat(parsed).timestamp() * 1000)
        except Exception:
            pass
    return int(time.time() * 1000)


def _normalize_choreography_event(event_data: dict[str, Any]) -> dict[str, Any]:
    if {
        "event",
        "choreography_id",
        "version",
        "updated_at",
    }.issubset(event_data.keys()):
        return event_data

    track: dict[str, Any] | None = None
    payload = event_data.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("track"), dict):
        track = payload["track"]
    elif isinstance(event_data.get("track"), dict):
        track = event_data["track"]
    elif isinstance(event_data.get("data"), dict):
        track = event_data["data"]
    elif isinstance(event_data.get("choreography"), dict):
        track = event_data["choreography"]

    if track is None:
        return event_data

    choreography_id = event_data.get("choreography_id") or track.get("id") or event_data.get("id")
    if choreography_id is None:
        return event_data

    updated_at = track.get("updated_at") or event_data.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at.strip():
        updated_at = datetime.now(timezone.utc).isoformat()

    version = _coerce_version(
        event_data.get("version") or track.get("version") or track.get("updated_version"),
        updated_at,
    )

    normalized_payload = payload if isinstance(payload, dict) else {"track": track}

    return {
        "event": event_data.get("event") or "choreography.updated",
        "choreography_id": str(choreography_id),
        "version": version,
        "updated_at": updated_at,
        "mode": event_data.get("mode") or "push",
        "payload": normalized_payload,
        "source": event_data.get("source") or "base44",
    }


def _normalize_routine_event(event_data: dict[str, Any]) -> dict[str, Any]:
    if {
        "event",
        "routine_id",
        "version",
        "updated_at",
    }.issubset(event_data.keys()):
        return event_data

    routine: dict[str, Any] | None = None
    payload = event_data.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("routine"), dict):
        routine = payload["routine"]
    elif isinstance(event_data.get("routine"), dict):
        routine = event_data["routine"]
    elif isinstance(event_data.get("data"), dict):
        routine = event_data["data"]

    if routine is None:
        return event_data

    routine_id = event_data.get("routine_id") or routine.get("id") or event_data.get("id")
    if routine_id is None:
        return event_data

    updated_at = routine.get("updated_at") or event_data.get("updated_at")
    if not isinstance(updated_at, str) or not updated_at.strip():
        updated_at = datetime.now(timezone.utc).isoformat()

    version = _coerce_version(
        event_data.get("version") or routine.get("version") or routine.get("updated_version"),
        updated_at,
    )

    normalized_payload = payload if isinstance(payload, dict) else {"routine": routine}

    return {
        "event": event_data.get("event") or "routine.updated",
        "routine_id": str(routine_id),
        "version": version,
        "updated_at": updated_at,
        "mode": event_data.get("mode") or "push",
        "payload": normalized_payload,
        "source": event_data.get("source") or "base44",
    }


def fetch_track_by_base44_id(base44_id: str) -> dict[str, Any]:
    headers = {"api_key": Config.BASE44_API_KEY or "", "Content-Type": "application/json"}
    list_url = f"{Config.BASE44_API_URL}/apps/{Config.BASE44_APP_ID}/entities/Track"
    detail_url = f"{list_url}/{base44_id}"

    if not Config.BASE44_API_KEY or not Config.BASE44_APP_ID:
        raise RuntimeError("Missing BASE44_API_KEY or BASE44_APP_ID for notify mode.")

    detail_resp = requests.get(detail_url, headers=headers, timeout=30)
    if detail_resp.ok:
        body = detail_resp.json()
        if isinstance(body, dict):
            return body

    list_resp = requests.get(list_url, headers=headers, timeout=30)
    list_resp.raise_for_status()
    rows = list_resp.json()
    if not isinstance(rows, list):
        raise RuntimeError("Unexpected base44 Track list response.")

    for row in rows:
        if isinstance(row, dict) and str(row.get("id", "")).strip() == base44_id:
            return row

    raise RuntimeError(f"Track {base44_id} not found in base44.")


def upsert_track_choreography(track: dict[str, Any], base44_id: str) -> dict[str, Any]:
    choreography = track.get("choreography")
    cues = track.get("cues")
    notes = track.get("notes")

    conn = psycopg2.connect(Config.get_db_connection_string())
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM tracks WHERE base44_id = %s", (base44_id,))
            row = cur.fetchone()

            if row:
                cur.execute(
                    """
                    UPDATE tracks
                    SET choreography = %s,
                        cues = %s,
                        notes = COALESCE(%s, notes),
                        synced_at = CURRENT_TIMESTAMP,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE base44_id = %s
                    RETURNING id
                    """,
                    (
                        Json(choreography) if choreography is not None else None,
                        Json(cues) if cues is not None else None,
                        notes,
                        base44_id,
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    raise RuntimeError(f"Failed to update track {base44_id}.")
                track_id = row[0]
                conn.commit()
                return {"action": "updated", "track_id": track_id}

            title = str(track.get("title") or "").strip()
            if not title:
                raise RuntimeError(
                    f"Track {base44_id} not found locally and payload missing title for insert."
                )

            cur.execute(
                """
                INSERT INTO tracks (
                    base44_id,
                    title,
                    artist,
                    choreography,
                    cues,
                    notes,
                    synced_at,
                    updated_at,
                    created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                RETURNING id
                """,
                (
                    base44_id,
                    title,
                    track.get("artist"),
                    Json(choreography) if choreography is not None else None,
                    Json(cues) if cues is not None else None,
                    notes,
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError(f"Failed to insert track {base44_id}.")
            track_id = row[0]
            conn.commit()
            return {"action": "inserted", "track_id": track_id}
    finally:
        conn.close()


def resolve_push_payload(base44_id: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        raise RuntimeError("push mode requires payload.")

    if isinstance(payload.get("track"), dict):
        track = payload["track"]
    else:
        track = payload

    if not isinstance(track, dict):
        raise RuntimeError("Invalid push payload: expected object.")

    if track.get("id") is None:
        track = {**track, "id": base44_id}

    return track


def fetch_routine_by_base44_id(base44_id: str) -> dict[str, Any]:
    headers = {"api_key": Config.BASE44_API_KEY or "", "Content-Type": "application/json"}
    list_url = f"{Config.BASE44_API_URL}/apps/{Config.BASE44_APP_ID}/entities/Routine"
    detail_url = f"{list_url}/{base44_id}"

    if not Config.BASE44_API_KEY or not Config.BASE44_APP_ID:
        raise RuntimeError("Missing BASE44_API_KEY or BASE44_APP_ID for notify mode.")

    detail_resp = requests.get(detail_url, headers=headers, timeout=30)
    if detail_resp.ok:
        body = detail_resp.json()
        if isinstance(body, dict):
            return body

    list_resp = requests.get(list_url, headers=headers, timeout=30)
    list_resp.raise_for_status()
    rows = list_resp.json()
    if not isinstance(rows, list):
        raise RuntimeError("Unexpected base44 Routine list response.")

    for row in rows:
        if isinstance(row, dict) and str(row.get("id", "")).strip() == base44_id:
            return row

    raise RuntimeError(f"Routine {base44_id} not found in base44.")


def resolve_push_routine_payload(
    base44_id: str, payload: dict[str, Any] | None
) -> dict[str, Any]:
    if payload is None:
        raise RuntimeError("push mode requires payload.")

    if isinstance(payload.get("routine"), dict):
        routine = payload["routine"]
    else:
        routine = payload

    if not isinstance(routine, dict):
        raise RuntimeError("Invalid push payload: expected object.")

    if routine.get("id") is None:
        routine = {**routine, "id": base44_id}

    return routine


def extract_track_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            tid = item.strip()
            if tid:
                out.append(tid)
            continue
        if isinstance(item, dict):
            for key in ("id", "base44_id", "track_base44_id"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    out.append(candidate.strip())
                    break
    return out


def upsert_routine(
    routine: dict[str, Any], base44_id: str, replace_tracks: bool = True
) -> dict[str, Any]:
    name = routine.get("name") or None
    description = routine.get("description") or None
    theme = routine.get("theme") or None
    intensity_arc = routine.get("intensity_arc") or None
    resistance_scale_notes = routine.get("resistance_scale_notes") or None
    class_summary = routine.get("class_summary") or None
    total_duration_minutes = routine.get("total_duration_minutes") or None
    difficulty = routine.get("difficulty") or None
    spotify_playlist_id = routine.get("spotify_playlist_id") or None
    tags = routine.get("tags")
    tags_json = Json(tags) if isinstance(tags, list) else None
    track_ids = extract_track_ids(routine.get("track_ids"))

    conn = psycopg2.connect(Config.get_db_connection_string())
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, name
                FROM routines
                WHERE base44_id = %s
                """,
                (base44_id,),
            )
            existing = cur.fetchone()
            existing_name = existing[1] if existing else None
            effective_name = name or existing_name
            if not effective_name:
                raise RuntimeError(
                    f"Routine {base44_id} missing required field 'name' for insert."
                )

            cur.execute(
                """
                INSERT INTO routines (
                    base44_id, name, description, theme, intensity_arc,
                    resistance_scale_notes, class_summary, total_duration_minutes,
                    difficulty, spotify_playlist_id, tags, synced_at, updated_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (base44_id)
                DO UPDATE SET
                    name = COALESCE(EXCLUDED.name, routines.name),
                    description = COALESCE(EXCLUDED.description, routines.description),
                    theme = COALESCE(EXCLUDED.theme, routines.theme),
                    intensity_arc = COALESCE(EXCLUDED.intensity_arc, routines.intensity_arc),
                    resistance_scale_notes = COALESCE(EXCLUDED.resistance_scale_notes, routines.resistance_scale_notes),
                    class_summary = COALESCE(EXCLUDED.class_summary, routines.class_summary),
                    total_duration_minutes = COALESCE(EXCLUDED.total_duration_minutes, routines.total_duration_minutes),
                    difficulty = COALESCE(EXCLUDED.difficulty, routines.difficulty),
                    spotify_playlist_id = COALESCE(EXCLUDED.spotify_playlist_id, routines.spotify_playlist_id),
                    tags = COALESCE(EXCLUDED.tags, routines.tags),
                    synced_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING id, (xmax = 0) AS inserted
                """,
                (
                    base44_id,
                    effective_name,
                    description,
                    theme,
                    intensity_arc,
                    resistance_scale_notes,
                    class_summary,
                    total_duration_minutes,
                    difficulty,
                    spotify_playlist_id,
                    tags_json,
                ),
            )
            row = cur.fetchone()
            if row is None:
                raise RuntimeError(f"Failed to insert or update routine {base44_id}.")
            routine_id = row[0]
            was_inserted = bool(row[1])

            if replace_tracks:
                cur.execute("DELETE FROM routine_tracks WHERE routine_id = %s", (routine_id,))
                for order, track_base44_id in enumerate(track_ids, start=1):
                    cur.execute(
                        "SELECT id FROM tracks WHERE base44_id = %s",
                        (track_base44_id,),
                    )
                    trow = cur.fetchone()
                    track_id = trow[0] if trow else None
                    cur.execute(
                        """
                        INSERT INTO routine_tracks (routine_id, track_base44_id, track_id, track_order)
                        VALUES (%s, %s, %s, %s)
                        """,
                        (routine_id, track_base44_id, track_id, order),
                    )

            conn.commit()
            return {
                "action": "inserted" if was_inserted else "updated",
                "routine_id": routine_id,
                "track_count": len(track_ids) if replace_tracks else None,
            }
    finally:
        conn.close()


async def process_webhook_job(job: dict[str, Any]) -> None:
    entity = job.get("entity", "track")
    mode = job["mode"]
    payload = job["payload"]

    if entity == "routine":
        routine_id = job["routine_id"]
        if mode == "push":
            routine = resolve_push_routine_payload(routine_id, payload)
            replace_tracks = "track_ids" in routine
        else:
            routine = await asyncio.to_thread(fetch_routine_by_base44_id, routine_id)
            replace_tracks = True
        result = await asyncio.to_thread(upsert_routine, routine, routine_id, replace_tracks)
        logger.info(
            "Processed routine webhook: id=%s version=%s mode=%s action=%s",
            routine_id,
            job["version"],
            mode,
            result.get("action"),
        )
        return

    choreography_id = job["choreography_id"]
    if mode == "push":
        track = resolve_push_payload(choreography_id, payload)
    else:
        track = await asyncio.to_thread(fetch_track_by_base44_id, choreography_id)

    result = await asyncio.to_thread(upsert_track_choreography, track, choreography_id)
    logger.info(
        "Processed choreography webhook: id=%s version=%s mode=%s action=%s",
        choreography_id,
        job["version"],
        mode,
        result.get("action"),
    )


async def webhook_worker() -> None:
    while True:
        job = await webhook_queue.get()
        try:
            await process_webhook_job(job)
        except Exception:
            logger.exception(
                "Webhook job failed for entity=%s id=%s version=%s",
                job.get("entity", "track"),
                job.get("choreography_id") or job.get("routine_id"),
                job.get("version"),
            )
        finally:
            webhook_queue.task_done()


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
                        result = await session.read_resource(AnyUrl(uri))
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
                    result = await session.read_resource(AnyUrl(uri))
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


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global webhook_worker_task
    if webhook_worker_task is None or webhook_worker_task.done():
        webhook_worker_task = asyncio.create_task(webhook_worker())
    try:
        yield
    finally:
        if webhook_worker_task is None:
            return
        webhook_worker_task.cancel()
        try:
            await webhook_worker_task
        except asyncio.CancelledError:
            pass
        webhook_worker_task = None


app = FastAPI(
    title="Cycle MCP Server Web API",
    version="0.1.0",
    summary="API for playlist generation and choreography sync webhooks.",
    description=(
        "Combines MCP playlist generation, optional OpenAI curation, and "
        "Base44 choreography update webhooks."
    ),
    openapi_tags=[
        {
            "name": "health",
            "description": "Service liveness checks.",
        },
        {
            "name": "playlist",
            "description": "Generate class routines and track lists.",
        },
        {
            "name": "webhooks",
            "description": "Inbound Base44 webhook endpoints.",
        },
    ],
    lifespan=lifespan,
)


def get_db_connection():
    """Create and return a database connection."""
    return psycopg2.connect(Config.get_db_connection_string())


@app.get("/health")
@app.get(
    "/health",
    tags=["health"],
    summary="Health check",
    description="Returns service liveness status.",
)
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/routines/{routine_id}/tracks")
async def get_routine_tracks(
    routine_id: int,
    _auth: None = Depends(require_api_key),
) -> dict[str, Any]:
    """Get all tracks in a routine with name, artist, and track type."""
    try:
        conn = get_db_connection()
        cursor = conn.cursor(cursor_factory=RealDictCursor)
        
        # Query to get tracks for the routine with JOIN to get track details
        query = """
            SELECT 
                t.title as name,
                t.artist,
                t.track_type,
                rt.track_order
            FROM routine_tracks rt
            INNER JOIN tracks t ON rt.track_id = t.id
            WHERE rt.routine_id = %s
            ORDER BY rt.track_order
        """
        
        cursor.execute(query, (routine_id,))
        tracks = cursor.fetchall()
        
        cursor.close()
        conn.close()
        
        # Convert to list of dicts and remove track_order from response
        track_list = [
            {
                "name": track["name"],
                "artist": track["artist"],
                "track_type": track["track_type"],
            }
            for track in tracks
        ]
        
        return {
            "routine_id": routine_id,
            "tracks": track_list,
            "count": len(track_list),
        }
        
    except psycopg2.Error as e:
        raise HTTPException(
            status_code=500,
            detail=f"Database error: {str(e)}",
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching routine tracks: {str(e)}",
        ) from e


@app.post("/api/playlist")
@app.post(
    "/api/playlist",
    tags=["playlist"],
    summary="Generate routine payload",
    description=(
        "Builds a routine payload using MCP playlist generation and optional OpenAI "
        "curation. Requires `X-API-Key`."
    ),
    responses={
        401: {"description": "Missing or invalid X-API-Key."},
        502: {"description": "Upstream MCP/OpenAI failure."},
    },
)
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


@app.post(
    "/api/tracks",
    tags=["playlist"],
    summary="Generate track list",
    description=(
        "Builds a structured track list suitable for Spotify enrichment. "
        "Requires `X-API-Key`."
    ),
    responses={
        401: {"description": "Missing or invalid X-API-Key."},
        502: {"description": "Upstream MCP/OpenAI failure."},
    },
)
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


@app.post(
    "/api/v1/choreography/updated",
    tags=["webhooks"],
    summary="Receive choreography update webhook",
    description=(
        "Receives `choreography.updated` events, verifies HMAC signature, applies "
        "idempotency and version checks, then queues async processing."
    ),
    responses={
        200: {"description": "Accepted or duplicate event."},
        400: {"description": "Invalid payload."},
        401: {"description": "Invalid or missing webhook signature/timestamp."},
        409: {"description": "Stale choreography version."},
    },
)
async def choreography_updated_webhook(
    request: Request,
    x_webhook_id: str | None = Header(
        default=None,
        description="Optional unique event ID for deduplication.",
    ),
    x_webhook_timestamp: str | None = Header(
        default=None,
        description="Unix timestamp used in webhook signature verification.",
    ),
    x_webhook_signature: str | None = Header(
        default=None,
        description=(
            "HMAC signature. Accepted formats: `sha256=<hex>`, `v1=<hex>`, "
            "`sha256:<hex>`, raw hex, or base64 digest."
        ),
    ),
) -> dict[str, Any]:
    raw_body = await request.body()
    require_webhook_signature(
        request=request,
        raw_body=raw_body,
        x_webhook_timestamp=x_webhook_timestamp,
        x_webhook_signature=x_webhook_signature,
    )

    try:
        event_data = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc

    if isinstance(event_data, dict):
        event_data = _normalize_choreography_event(event_data)

    try:
        event = ChoreographyWebhookEvent.model_validate(event_data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid webhook payload: {exc}") from exc

    if event.event != "choreography.updated":
        raise HTTPException(status_code=400, detail="Unsupported event.")

    mode = event.mode.strip().lower()
    if mode not in {"notify", "push"}:
        raise HTTPException(status_code=400, detail="mode must be 'notify' or 'push'.")

    if mode == "push" and event.payload is None:
        raise HTTPException(status_code=400, detail="push mode requires payload.")

    event_id = (x_webhook_id or "").strip()
    if not event_id:
        for key in ("event_id", "webhook_id", "id"):
            candidate = event_data.get(key) if isinstance(event_data, dict) else None
            if isinstance(candidate, str) and candidate.strip():
                event_id = candidate.strip()
                break

    if not event_id:
        # Stable fallback for providers that do not send an explicit webhook ID.
        basis = f"{event.event}|{event.choreography_id}|{event.version}"
        event_id = hashlib.sha256(basis.encode("utf-8")).hexdigest()

    classification = await webhook_state_store.classify_and_mark(
        event_id=event_id,
        choreography_id=f"track:{event.choreography_id}",
        version=event.version,
    )
    if classification == "duplicate":
        return {"status": "duplicate", "accepted": False}
    if classification == "stale":
        raise HTTPException(
            status_code=409,
            detail="Stale choreography version.",
        )

    await webhook_queue.put(
        {
            "entity": "track",
            "event_id": event_id,
            "choreography_id": event.choreography_id,
            "version": event.version,
            "updated_at": event.updated_at,
            "mode": mode,
            "payload": event.payload,
            "source": event.source,
        }
    )

    return {
        "status": "accepted",
        "accepted": True,
        "queue_depth": webhook_queue.qsize(),
    }


@app.post(
    "/api/v1/routine/updated",
    tags=["webhooks"],
    summary="Receive routine update webhook",
    description=(
        "Receives `routine.updated` events, verifies HMAC signature, applies "
        "idempotency and version checks, then queues async processing."
    ),
    responses={
        200: {"description": "Accepted or duplicate event."},
        400: {"description": "Invalid payload."},
        401: {"description": "Invalid or missing webhook signature/timestamp."},
        409: {"description": "Stale routine version."},
    },
)
async def routine_updated_webhook(
    request: Request,
    x_webhook_id: str | None = Header(
        default=None,
        description="Optional unique event ID for deduplication.",
    ),
    x_webhook_timestamp: str | None = Header(
        default=None,
        description="Unix timestamp used in webhook signature verification.",
    ),
    x_webhook_signature: str | None = Header(
        default=None,
        description=(
            "HMAC signature. Accepted formats: `sha256=<hex>`, `v1=<hex>`, "
            "`sha256:<hex>`, raw hex, or base64 digest."
        ),
    ),
) -> dict[str, Any]:
    raw_body = await request.body()
    require_webhook_signature(
        request=request,
        raw_body=raw_body,
        x_webhook_timestamp=x_webhook_timestamp,
        x_webhook_signature=x_webhook_signature,
    )

    try:
        event_data = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc

    if isinstance(event_data, dict):
        event_data = _normalize_routine_event(event_data)

    try:
        event = RoutineWebhookEvent.model_validate(event_data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid webhook payload: {exc}") from exc

    if event.event != "routine.updated":
        raise HTTPException(status_code=400, detail="Unsupported event.")

    mode = event.mode.strip().lower()
    if mode not in {"notify", "push"}:
        raise HTTPException(status_code=400, detail="mode must be 'notify' or 'push'.")

    if mode == "push" and event.payload is None:
        raise HTTPException(status_code=400, detail="push mode requires payload.")

    event_id = (x_webhook_id or "").strip()
    if not event_id:
        for key in ("event_id", "webhook_id", "id"):
            candidate = event_data.get(key) if isinstance(event_data, dict) else None
            if isinstance(candidate, str) and candidate.strip():
                event_id = candidate.strip()
                break
    if not event_id:
        basis = f"{event.event}|{event.routine_id}|{event.version}"
        event_id = hashlib.sha256(basis.encode("utf-8")).hexdigest()

    classification = await webhook_state_store.classify_and_mark(
        event_id=event_id,
        choreography_id=f"routine:{event.routine_id}",
        version=event.version,
    )
    if classification == "duplicate":
        return {"status": "duplicate", "accepted": False}
    if classification == "stale":
        raise HTTPException(status_code=409, detail="Stale routine version.")

    await webhook_queue.put(
        {
            "entity": "routine",
            "event_id": event_id,
            "routine_id": event.routine_id,
            "version": event.version,
            "updated_at": event.updated_at,
            "mode": mode,
            "payload": event.payload,
            "source": event.source,
        }
    )
    return {
        "status": "accepted",
        "accepted": True,
        "queue_depth": webhook_queue.qsize(),
    }
