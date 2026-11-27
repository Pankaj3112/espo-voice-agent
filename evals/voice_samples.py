"""Render the same agent lines in several TTS voices and record time-to-first-audio.

Writes WAVs + index.json under evals/results/voices/. Usage:
  uv run python evals/voice_samples.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import aiohttp  # noqa: E402
from livekit.agents import inference  # noqa: E402
from livekit.plugins import deepgram, elevenlabs  # noqa: E402

from espo_voice_agent import config  # noqa: E402,F401  (loads secrets)

TEXT = (
    "Got it. So that's Jane Doe from Acme Robotics, phone four one five, five five five, zero one four two, "
    "and email jane dot doe at acme dot com. Shall I add that?"
)
OUT = Path(__file__).parent / "results" / "voices"

CANDIDATES = [
    ("ElevenLabs Flash v2.5 · Bella (current)", lambda s: elevenlabs.TTS(model="eleven_flash_v2_5", voice_id="hpp4J3VqNfWAUOO0d1Us", http_session=s)),
    ("ElevenLabs Turbo v2.5 · Bella", lambda s: elevenlabs.TTS(model="eleven_turbo_v2_5", voice_id="hpp4J3VqNfWAUOO0d1Us", http_session=s)),
    ("Cartesia Sonic 3.5", lambda s: inference.TTS(model="cartesia/sonic-3.5", http_session=s)),
    ("Cartesia Sonic 3", lambda s: inference.TTS(model="cartesia/sonic-3", http_session=s)),
    ("Fish Audio S2.1 Pro", lambda s: inference.TTS(model="fishaudio/s2.1-pro", http_session=s)),
    ("Inworld TTS 2", lambda s: inference.TTS(model="inworld/inworld-tts-2", http_session=s)),
    ("Inworld TTS 2 Flash", lambda s: inference.TTS(model="inworld/inworld-tts-2-flash", http_session=s)),
    ("Rime Arcana (coda)", lambda s: inference.TTS(model="rime/coda", http_session=s)),
    ("Deepgram Aura-2 · Thalia", lambda s: deepgram.TTS(model="aura-2-thalia-en", http_session=s)),
]


async def render(name: str, make, session: aiohttp.ClientSession) -> dict:
    tts = make(session)
    t0 = time.perf_counter()
    ttfb = None
    frames: list[bytes] = []
    sr = 24000
    try:
        async with tts.synthesize(TEXT) as stream:
            async for ev in stream:
                if ttfb is None:
                    ttfb = time.perf_counter() - t0
                sr = ev.frame.sample_rate
                frames.append(bytes(ev.frame.data))
    except Exception as e:  # keep going; report the failure
        return {"name": name, "error": repr(e)[:300]}
    slug = "".join(c if c.isalnum() else "-" for c in name.lower()).strip("-")
    path = OUT / f"{slug}.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(b"".join(frames))
    return {"name": name, "file": path.name, "ttfb_s": round(ttfb or 0, 3), "total_s": round(time.perf_counter() - t0, 3)}


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    only = sys.argv[1:]
    async with aiohttp.ClientSession() as s:
        rows = []
        for name, make in CANDIDATES:
            if only and not any(o.lower() in name.lower() for o in only):
                continue
            r = await render(name, make, s)
            print(r)
            rows.append(r)
    (OUT / "index.json").write_text(json.dumps({"text": TEXT, "rows": rows}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
