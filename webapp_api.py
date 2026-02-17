import os
import json
from typing import Any, Optional

import httpx
from fastapi import FastAPI, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from openai import OpenAI, DefaultHttpxClient

# -----------------------
# App
# -----------------------
app = FastAPI()

# -----------------------
# Auth
# -----------------------
WEBAPP_API_KEY = os.getenv("WEBAPP_API_KEY", "").strip()

def require_api_key(x_api_key: str = Header(...)) -> None:
    if not WEBAPP_API_KEY:
        raise HTTPException(status_code=500, detail="WEBAPP_API_KEY not configured on server")
    if x_api_key != WEBAPP_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")

# -----------------------
# OpenAI
# -----------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL_DRAFT = os.getenv("OPENAI_MODEL_DRAFT", "gpt-4o-mini")
OPENAI_MODEL_REFINE = os.getenv("OPENAI_MODEL_REFINE", "gpt-4o")

client = OpenAI(
    api_key=OPENAI_API_KEY,
    http_client=DefaultHttpxClient(
        timeout=httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=30.0),
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
    ),
)

# -----------------------
# Models
# -----------------------
class PlaylistChatRequest(BaseModel):
    conversation_id: Optional[str] = None
    lock: bool = False
    inputs: dict[str, Any] = Field(default_factory=dict)
    draft_playlist: dict[str, Any] = Field(default_factory=lambda: {"tracks": []})
    user_message: str = ""

class PlaylistChatResponse(BaseModel):
    assistant_message: str
    stage: str
    questions: list[str] = Field(default_factory=list)
    draft_playlist: dict[str, Any]
    final_playlist: Optional[dict[str, Any]] = None

# -----------------------
# Endpoint (stub for now)
# -----------------------
@app.post("/api/playlist/chat", tags=["playlist"])
async def playlist_chat(
    request_data: PlaylistChatRequest,
    _auth: None = Depends(require_api_key),
) -> PlaylistChatResponse:
    if not OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY not configured on server")

    # TODO: plug OpenAI call here after this boots cleanly
    draft = request_data.draft_playlist or {"tracks": []}
    stage = "final" if request_data.lock else "refining"

    return PlaylistChatResponse(
        assistant_message="Stub OK (server running). Next: wire OpenAI call.",
        stage=stage,
        questions=[],
        draft_playlist=draft,
        final_playlist=draft if request_data.lock else None,
    )
