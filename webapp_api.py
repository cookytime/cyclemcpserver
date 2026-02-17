import os
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from openai import OpenAI, DefaultHttpxClient

# ============================================
# FastAPI App
# ============================================

app = FastAPI()

# ============================================
# API Key Protection
# ============================================

API_KEY = os.getenv("WEBAPP_API_KEY", "change-me")


def require_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

# ============================================
# OpenAI Cofiguration
# ============================================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL_DRAFT = os.getenv("OPENAI_MODEL_DRAFT", "gpt-4o-mini")
OPENAI_MODEL_REFINE = os.getenv("OPENAI_MODEL_REFINE", "gpt-4o")

_openai_client = OpenAI(
    api_key=OPENAI_API_KEY,
    http_client=DefaultHttpxClient(
        timeout=httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=30.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    ),
)

# ============================================
# Models
# ============================================


class PlaylistChatRequest(BaseModel):
    conversation_id: str | None = None
    lock: bool = False
    inputs: dict[str, Any] = Field(default_factory=dict)
    draft_playlist: dict[str, Any] = Field(default_factory=lambda: {"tracks": []})
    user_message: str = ""


class PlaylistChat:contentReference[oaicite:6]{index=6}s:contentReference[oaicite:7]{index=7} str
    questions: list[str] = Field(default_factory=list)
    draft_playlist: dict[str, Any]
    final_playlist: dict[str, Any] | None = None


def _s(val: Any) -> str:
    return val.strip() if isinstance(val, str) else ""


def _with_default(val: Any, fallback: str) -> str:
    s = _s(val)
    return s if s else fallback


def _extract_remove_artists(msg: str) -> list[str]:
    """
    Very simple parser:
    - "remove taylor swift"
    - "no taylor swift"
    - "exclude taylor swift"
    - "ban taylor swift"
    Returns list of artist strings.
    """
    if not msg:
        return []
    m = msg.lower()
    triggers = ["remove ", "no ", "exclude ", "ban "]
    found: list[str] = []
    for t in triggers:
        if t in m:
            # take substring after trigger up to punctuation/newline
            parts = m.split(t)
            for p in parts[1:]:
                chunk = p.split("\n")[0].split(".")[0].split(",")[0].strip()
                if chunk and len(chunk) <= 60:
                    found.append(chunk)
    # dedupe preserving order
    out: list[str] = []
    seen = set()
    for a in found:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return out


def _filter_banned_artists(tracks: list[dict[str, Any]], banned: list[str]) -> list[dict[str, Any]]:
    if not banned:
        return tracks
    banned_l = {b.lower().strip() for b in banned if b.strip()}
    out = []
    for t in tracks:
        artist = str(t.get("artist", "")).lower().strip()
        if artist and artist in banned_l:
            continue
        out.append(t)
    return out


@app.post("/api/playlist/chat", tags=["playlist"])
async def playlist_chat(
    request_data: PlaylistChatRequest,
    _auth: None = Depends(require_api_key),
) -> PlaylistChatResponse:
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY not configured on server")

    # ---- Defaults for “prefill” from your page ----
    inputs = request_data.inputs or {}
    merged_inputs = {
        "theme": _with_default(inputs.get("theme"), "General motivating ride"),
        "vibe": _with_default(inputs.get("vibe"), "High-energy and empowering"),
        "audience": _with_default(inputs.get("audience"), "General fitness riders"),
        "intensity_arc": _with_default(inputs.get("intensity_arc"), "Warmup → Build → Peaks → Cooldown"),
        "preferred_genres": _with_default(inputs.get("preferred_genres"), "Pop, Rock"),
        "preferred_artists": _s(inputs.get("preferred_artists")),
        "excluded_genres": _s(inputs.get("excluded_genres")),
        "banned_artists": inputs.get("banned_artists") or [],
    }

    # Add “remove artist” requests into banned list so it STICKS
    removed_from_message = _extract_remove_artists(request_data.user_message or "")
    banned_artists = list(merged_inputs["banned_artists"]) + removed_from_message

    current_tracks = (request_data.draft_playlist or {}).get("tracks") or []
    model = OPENAI_MODEL_REFINE if current_tracks else OPENAI_MODEL_DRAFT

    # ---- Structured Outputs schema (must have additionalProperties:false everywhere) ----
    playlist_obj_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tracks": {
                "type": "array",
                "minItems": 10,
                "maxItems": 15,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "artist": {"type": "string"},
                        "estimated_bpm": {"type": "number"},
                        "energy": {"type": "number"},
                        "segment": {"type": "string", "enum": ["warmup", "build", "peak", "recovery", "cooldown"]},
                        "why_it_fits": {"type": "string"},
                    },
                    "required": ["title", "artist", "estimated_bpm", "energy", "segment", "why_it_fits"],
                },
            }
        },
        "required": ["tracks"],
    }

    output_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "assistant_message": {"type": "string"},
            "stage": {"type": "string", "enum": ["drafting", "refining", "final"]},
            "questions": {"type": "array", "items": {"type": "string"}},
            "draft_playlist": playlist_obj_schema,
            "final_playlist": {
                "anyOf": [
                    {"type": "null"},
                    playlist_obj_schema,
                ]
            },
        },
        "required": ["assistant_message", "stage", "questions", "draft_playlist", "final_playlist"],
    }

    instructions = f"""
You are a cycling class playlist coach. Iterate until the user is happy, then finalize.

LOCK requested: {"true" if request_data.lock else "false"}

Inputs:
- Theme: {merged_inputs["theme"]}
- Vibe: {merged_inputs["vibe"]}
- Audience: {merged_inputs["audience"]}
- Intensity Arc: {merged_inputs["intensity_arc"]}
- Preferred Genres: {merged_inputs["preferred_genres"]}
- Preferred Artists: {merged_inputs["preferred_artists"] or "(none)"}
- Excluded Genres: {merged_inputs["excluded_genres"] or "(none)"}

Hard bans:
- BANNED ARTISTS: {banned_artists if banned_artists else "(none)"}
You MUST NOT include any banned artist in any playlist.

Current draft playlist (edit this; do NOT rewrite everything unless user asks):
{json.dumps(current_tracks, ensure_ascii=False)}

Rules:
- 10–15 tracks total
- Follow the intensity arc
- Keep excluded genres STRICT
- Keep cohesive and aligned with theme+vibe
- If user asks for a change, modify minimally (swap only what’s needed)
- If lock=true OR user intent is finalize, stage="final" and final_playlist must be set
"""

    # Messages: keep it simple (last instruction + user message)
    user_msg = (request_data.user_message or "").strip() or "Generate a first draft playlist."
    resp = _openai_client.responses.create(
        model=model,
        instructions=instructions,
        input=[{"role": "user", "content": user_msg}],
        text={
            "format": {
                "type": "json_schema",
                "name": "playlist_chat_response",
                "schema": output_schema,
                "strict": True,
            }
        },
        temperature=0.8,
    )

    raw = getattr(resp, "output_text", "")  # SDK convenience :contentReference[oaicite:8]{index=8}
    if not raw:
        raise HTTPException(status_code=502, detail="OpenAI returned no output_text")

    try:
        data = json.loads(raw)
    except Exception:
        raise HTTPException(status_code=502, detail=f"OpenAI did not return valid JSON: {raw[:300]}")

    # Enforce bans server-side too (belt + suspenders)
    data["draft_playlist"]["tracks"] = _filter_banned_artists(data["draft_playlist"]["tracks"], banned_artists)

    if request_data.lock and data.get("stage") != "final":
        data["stage"] = "final"
        data["final_playlist"] = data.get("final_playlist") or data["draft_playlist"]

    if data.get("final_playlist"):
        data["final_playlist"]["tracks"] = _filter_banned_artists(data["final_playlist"]["tracks"], banned_artists)

    return PlaylistChatResponse(
        assistant_message=data["assistant_message"],
        stage=data["stage"],
        questions=data.get("questions") or [],
        draft_playlist=data["draft_playlist"],
        final_playlist=data.get("final_playlist"),
    )

# ============================================
# Playlist Chat Endpoint
# ============================================


@app.post("/api/playlist/chat", tags=["playlist"])
async def playlist_chat(
    request_data: PlaylistChatRequest,
    _auth: None = Depends(require_api_key),
) -> PlaylistChatResponse:

    # Extract state
    lock = request_data.lock
    inputs = request_data.inputs
    draft = request_data.draft_playlist
    user_message = request_data.user_message

    # 🔥 TODO: Call OpenAI here (we'll plug this next)

    # Temporary stub so server runs clean
    result = {
        "assistant_message": "Playlist updated successfully.",
        "stage": "final" if lock else "refining",
        "questions": [],
        "draft_playlist": draft,
        "final_playlist": draft if lock else None,
    }

    return PlaylistChatResponse(**result)
