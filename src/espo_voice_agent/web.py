"""Tiny web server: serves the demo page and mints LiveKit tokens.

Each "Start call" creates a fresh room and dispatches the agent into it. Basic
guards keep a public link from burning through free-tier credits.

Run: uv run uvicorn espo_voice_agent.web:app --port 8790
"""

from __future__ import annotations

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


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


def _client_ip(req: Request) -> str:
    # Behind Tailscale Funnel / a reverse proxy the original client is in X-Forwarded-For.
    fwd = req.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (req.client.host if req.client else "unknown")


async def _active_calls(lk: api.LiveKitAPI) -> int:
    rooms = await lk.room.list_rooms(api.ListRoomsRequest())
    return sum(1 for r in rooms.rooms if r.name.startswith(ROOM_PREFIX) and r.num_participants > 0)


@app.post("/api/session")
async def create_session(req: Request) -> dict:
    ip = _client_ip(req)
    hits = _ip_hits[ip]
    now = time.time()
    while hits and now - hits[0] > 3600:
        hits.popleft()
    if len(hits) >= PER_IP_LIMIT:
        raise HTTPException(429, "You've hit the demo's hourly call limit. Please try again a bit later.")

    async with api.LiveKitAPI() as lk:  # reads LIVEKIT_URL / _API_KEY / _API_SECRET
        if await _active_calls(lk) >= config.MAX_CONCURRENT_CALLS:
            raise HTTPException(503, "The demo is busy with other calls right now. Please try again in a few minutes.")

    hits.append(now)
    room = ROOM_PREFIX + secrets.token_hex(4)
    identity = "caller-" + secrets.token_hex(3)
    token = (
        api.AccessToken()
        .with_identity(identity)
        .with_name("Caller")
        .with_ttl(timedelta(minutes=10))
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
