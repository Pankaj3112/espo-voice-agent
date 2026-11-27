"""Tiny web server: serves the demo page and mints LiveKit tokens.

Each "Start call" creates a fresh room and dispatches the agent into it. Basic
guards keep a public link from burning through free-tier credits.

Run: uv run uvicorn espo_voice_agent.web:app --port 8790
"""

from __future__ import annotations

import asyncio

import os
import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from livekit import api

from . import config

STATIC = Path(__file__).parent / "static"
ROOM_PREFIX = "intake-"
PER_IP_LIMIT = 8  # calls per IP per hour
_ip_hits: dict[str, deque[float]] = defaultdict(deque)

app = FastAPI(title="espo-voice-agent", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/notes.pdf")
async def notes() -> FileResponse:
    return FileResponse(STATIC / "notes.pdf", media_type="application/pdf", filename="ava-technical-notes.pdf",
                        content_disposition_type="inline")


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


def _client_ip(req: Request) -> str:
    # Behind Tailscale Funnel / a reverse proxy the original client is in X-Forwarded-For.
    fwd = req.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (req.client.host if req.client else "unknown")


async def _active_calls(lk: api.LiveKitAPI) -> int:
    """Rooms with a real caller in them. The agent can linger in a room for a few seconds after the
    caller hangs up, so rooms are counted by their non-agent participants, not by size."""
    rooms = await lk.room.list_rooms(api.ListRoomsRequest())
    n = 0
    for r in rooms.rooms:
        if not r.name.startswith(ROOM_PREFIX) or r.num_participants == 0:
            continue
        parts = await lk.room.list_participants(api.ListParticipantsRequest(room=r.name))
        if any(pp.kind != api.ParticipantInfo.Kind.AGENT for pp in parts.participants):
            n += 1
    return n


_admit_lock = asyncio.Lock()
_recent_tokens: deque[float] = deque()  # issued tokens whose caller may not have joined yet
_RESERVE_S = 45


@app.post("/api/session")
async def create_session(req: Request) -> dict:
    ip = _client_ip(req)
    # One admission at a time: both quotas are checked and reserved atomically, so concurrent or rapid
    # requests can't all see the same free slot.
    async with _admit_lock:
        hits = _ip_hits[ip]
        now = time.time()
        while hits and now - hits[0] > 3600:
            hits.popleft()
        if len(hits) >= PER_IP_LIMIT:
            raise HTTPException(429, "You've hit the demo's hourly call limit. Please try again a bit later.")
        while _recent_tokens and now - _recent_tokens[0] > _RESERVE_S:
            _recent_tokens.popleft()
        async with api.LiveKitAPI() as lk:  # reads LIVEKIT_URL / _API_KEY / _API_SECRET
            in_rooms = await _active_calls(lk)
        # Rooms only show a caller once they join; tokens issued in the last ~45 s count as reserved meanwhile.
        if max(in_rooms, len(_recent_tokens)) >= config.MAX_CONCURRENT_CALLS:
            raise HTTPException(503, "The demo is busy with other calls right now. Please try again in a few minutes.")
        hits.append(now)
        _recent_tokens.append(now)
    room = ROOM_PREFIX + secrets.token_hex(4)
    identity = "caller-" + secrets.token_hex(3)
    token = (
        api.AccessToken()
        .with_identity(identity)
        .with_name("Caller")
        .with_ttl(timedelta(seconds=60))  # join right away; an old token can't start another call
        .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True, can_publish_data=True))
        .with_room_config(
            api.RoomConfiguration(
                agents=[api.RoomAgentDispatch(agent_name=config.AGENT_NAME)],
                empty_timeout=30,
                departure_timeout=5,
            )
        )
        .to_jwt()
    )
    return {
        "url": os.environ["LIVEKIT_URL"],
        "token": token,
        "room": room,
        "max_call_seconds": config.MAX_CALL_SECONDS,
        "espo_url": config.ESPO_BASE_URL,
    }
