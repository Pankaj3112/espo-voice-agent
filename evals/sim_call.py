"""End-to-end simulated phone call against the live agent.

A second LLM plays the caller (with a persona and the lead's facts), its lines
are synthesised to speech with Deepgram Aura and published into the LiveKit
room as a real microphone track. The agent under test hears audio only, exactly
like a browser caller. We then verify the outcome independently through the
EspoCRM REST API and record client-side response latency.

Usage:
  uv run python evals/sim_call.py --scenario happy_path
  uv run python evals/sim_call.py --all --out evals/results/sim_calls.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import secrets
import sys
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import httpx
import numpy as np
from livekit import api, rtc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from espo_voice_agent import config  # noqa: E402  (loads secrets)

SR = 48000
FRAME = SR // 100  # 10 ms
ESPO_API = config.ESPO_BASE_URL.rstrip("/") + "/api/v1"
ESPO_AUTH = ("admin", os.environ.get("ESPO_DEMO_PASSWORD", "1"))  # public demo credentials

FIRST = ["Maria", "Daniel", "Priya", "Lukas", "Sofia", "Kenji", "Amara", "Tomás", "Hannah", "Ravi"]
LAST = ["Gonzalez", "Whitaker", "Raman", "Brunner", "Okafor", "Lindqvist", "Moreau", "Castellano", "Novak", "Iyer"]
COMPANIES = ["Nimbus Analytics", "Bluefin Logistics", "Helvetic Dairy", "Orbit Robotics", "Quarry Labs"]


@dataclass
class Scenario:
    name: str
    persona: str  # how the caller behaves
    expect_created: bool = True
    facts: dict = field(default_factory=dict)


def make_facts(rng: random.Random) -> dict:
    first, last, company = rng.choice(FIRST), rng.choice(LAST), rng.choice(COMPANIES)
    dom = re.sub(r"[^a-z]", "", company.lower().split()[0]) + ".com"
    return {
        "first_name": first,
        "last_name": last,
        "company": company,
        "phone": f"+1 415 555 {rng.randint(1000, 9999)}",
        "email": f"{first.lower().replace('á', 'a')}.{last.lower()}@{dom}",
    }


SCENARIOS = {
    "happy_path": Scenario(
        "happy_path",
        "You are a sales rep. Give the name and company in your first sentence, then phone and email when asked "
        "(or right away). Confirm when the agent reads the details back correctly.",
    ),
    "one_breath": Scenario(
        "one_breath",
        "You are a sales rep in a hurry. Say ALL details (name, company, phone, email) in your very first sentence. "
        "Confirm the read-back.",
    ),
    "correction": Scenario(
        "correction",
        "You are a sales rep. First give the company as 'Nimbus Analytic' by mistake, then immediately in your next "
        "turn correct yourself to the real company. Give phone and email. Only confirm if the read-back has the "
        "corrected company.",
    ),
    "decline": Scenario(
        "decline",
        "You are a sales rep. Give the details, but when the agent reads them back say you changed your mind and do "
        "NOT want the lead created. Then say goodbye.",
        expect_created=False,
    ),
}

CALLER_SYS = """You are role-playing a caller talking to a CRM voice assistant on the phone.
Persona: {persona}
Lead facts (use exactly these): {facts}
Make sure you give the name, company, phone AND email at some point (unless your persona says otherwise). Never invent
other details; if asked for a job title or anything else, say you don't have it.
Speak like a person on the phone: short, natural sentences. Say phone numbers digit by digit and emails as
"x dot y at z dot com". Reply with ONLY what you say out loud. When the agent has confirmed the lead is saved (or
you declined) and asks if there's anything else, say a short goodbye and then output the single token <END> on
the next turn."""


NOISE = {"std": 0.0, "babble": 0.0}
BABBLE_TEXT = (
    "So anyway I told him the meeting was moved to Thursday, and he said that was fine. "
    "Did you see the game last night? It went to overtime again. I think we should order lunch soon, "
    "maybe the Thai place downstairs. The printer on the third floor is jammed again, someone should call facilities."
)


async def tts_pcm(text: str) -> np.ndarray:
    async with httpx.AsyncClient() as http:
        r = await http.post(
            "https://api.deepgram.com/v1/speak",
            params={"model": "aura-2-orion-en", "encoding": "linear16", "sample_rate": SR, "container": "none"},
            headers={"Authorization": f"Token {os.environ['DEEPGRAM_API_KEY']}"},
            json={"text": text},
            timeout=30,
        )
        r.raise_for_status()
        return np.frombuffer(r.content, dtype=np.int16)


class Caller:
    """Publishes TTS audio as a mic track; pads with silence so VAD sees real pauses."""

    def __init__(self, room: rtc.Room, noise_std: float = 0.0) -> None:
        self.room = room
        self.noise_std = noise_std  # >0 adds white noise (int16 std)
        self.babble: np.ndarray | None = None  # optional background talker, looped under everything
        self.babble_gain = 0.0
        self.source = rtc.AudioSource(SR, 1)
        self.queue: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self.speaking_done = asyncio.Event()
        self.speech_end_t = 0.0

    async def start(self) -> None:
        track = rtc.LocalAudioTrack.create_audio_track("caller-mic", self.source)
        await self.room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        rng = np.random.default_rng(0)

        pos = {"b": 0}

        def noisy(x: np.ndarray) -> np.ndarray:
            if not self.noise_std and self.babble is None:
                return x
            y = x.astype(np.float32)
            if self.noise_std:
                y += rng.normal(0, self.noise_std, x.size)
            if self.babble is not None:
                i = pos["b"]
                idx = (np.arange(x.size) + i) % self.babble.size
                y += self.babble[idx].astype(np.float32) * self.babble_gain
                pos["b"] = (i + x.size) % self.babble.size
            return np.clip(y, -32768, 32767).astype(np.int16)

        silence = np.zeros(FRAME, dtype=np.int16)
        while True:
            try:
                pcm = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pcm = None
            if pcm is None:
                await self.source.capture_frame(rtc.AudioFrame(noisy(silence).tobytes(), SR, 1, FRAME))
                continue
            for i in range(0, len(pcm), FRAME):
                chunk = pcm[i : i + FRAME]
                if len(chunk) < FRAME:
                    chunk = np.pad(chunk, (0, FRAME - len(chunk)))
                await self.source.capture_frame(rtc.AudioFrame(noisy(chunk).tobytes(), SR, 1, FRAME))
            # capture_frame returns once audio is queued (up to 1 s ahead), so wait until it has
            # actually been sent before stamping the caller's end of speech.
            await self.source.wait_for_playout()
            self.speech_end_t = time.perf_counter()
            self.speaking_done.set()

    async def say(self, text: str, http: httpx.AsyncClient) -> None:
        r = await http.post(
            "https://api.deepgram.com/v1/speak",
            params={"model": "aura-2-thalia-en", "encoding": "linear16", "sample_rate": SR, "container": "none"},
            headers={"Authorization": f"Token {os.environ['DEEPGRAM_API_KEY']}"},
            json={"text": text},
            timeout=30,
        )
        r.raise_for_status()
        self.speaking_done.clear()
        pcm = np.frombuffer(r.content, dtype=np.int16)
        # Drop the TTS clip's trailing silence so latency is measured from the last spoken word.
        loud = np.flatnonzero(np.abs(pcm) > 300)
        if loud.size:
            pcm = pcm[: loud[-1] + 1]
        await self.queue.put(pcm)
        await self.speaking_done.wait()


async def caller_llm(http: httpx.AsyncClient, messages: list[dict]) -> str:
    r = await http.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY']}"},
        json={"model": "openai/gpt-6-luna", "reasoning": {"effort": "none"}, "messages": messages, "temperature": 0.7},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


async def run_scenario(sc: Scenario, seed: int, verbose: bool = True) -> dict:
    rng = random.Random(seed)
    sc.facts = make_facts(rng)
    room_name = f"eval-{sc.name}-{secrets.token_hex(3)}"
    token = (
        api.AccessToken()
        .with_identity("sim-caller")
        .with_ttl(timedelta(minutes=10))
        .with_grants(api.VideoGrants(room_join=True, room=room_name))
        .with_room_config(api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=config.AGENT_NAME)]))
        .to_jwt()
    )
    room = rtc.Room()
    transcript: list[tuple[str, str]] = []
    events: list[dict] = []
    agent_state = {"v": "initializing", "changed": asyncio.Event()}
    first_audio_after: list[float] = []
    caller = Caller(room, noise_std=NOISE["std"])
    if NOISE["babble"]:
        caller.babble = await tts_pcm(BABBLE_TEXT)
        caller.babble_gain = NOISE["babble"]
    await_first_audio = {"armed": False, "t_end": 0.0}

    def on_attrs(changed, participant):
        s = participant.attributes.get("lk.agent.state")
        if s and s != agent_state["v"]:
            agent_state["v"] = s
            agent_state["changed"].set()

    room.on("participant_attributes_changed", on_attrs)

    async def watch_audio(track: rtc.RemoteAudioTrack):
        stream = rtc.AudioStream(track, sample_rate=SR, num_channels=1)
        async for ev in stream:
            if not await_first_audio["armed"]:
                continue
            pcm = np.frombuffer(ev.frame.data, dtype=np.int16)
            if pcm.size and np.sqrt(np.mean(pcm.astype(np.float32) ** 2)) > 300:
                first_audio_after.append(time.perf_counter() - await_first_audio["t_end"])
                await_first_audio["armed"] = False

    room.on("track_subscribed", lambda t, *_: asyncio.create_task(watch_audio(t)) if t.kind == rtc.TrackKind.KIND_AUDIO else None)

    def on_text(reader: rtc.TextStreamReader, identity: str):
        async def _read():
            txt = await reader.read_all()
            if reader.info.topic == "agent-events":
                events.append(json.loads(txt))
            elif identity != "sim-caller" and reader.info.attributes.get("lk.transcription_final") == "true":
                transcript.append(("agent", txt))

        asyncio.create_task(_read())

    room.register_text_stream_handler("lk.transcription", on_text)
    room.register_text_stream_handler("agent-events", on_text)

    t_start = time.perf_counter()
    await room.connect(os.environ["LIVEKIT_URL"], token)
    await caller.start()

    async def wait_agent_turn(timeout=45.0) -> bool:
        """Wait until the agent has spoken and returned to listening (and stays there briefly)."""
        deadline = time.perf_counter() + timeout
        spoke = False
        while time.perf_counter() < deadline:
            agent_state["changed"].clear()
            if agent_state["v"] in ("speaking", "thinking"):
                spoke = True
            if spoke and agent_state["v"] == "listening":
                try:
                    await asyncio.wait_for(agent_state["changed"].wait(), 1.2)
                    continue  # state moved again (e.g. tool → speaking); keep waiting
                except asyncio.TimeoutError:
                    return True
            try:
                await asyncio.wait_for(agent_state["changed"].wait(), 1.0)
            except asyncio.TimeoutError:
                pass
        return False

    msgs = [{"role": "system", "content": CALLER_SYS.format(persona=sc.persona, facts=json.dumps(sc.facts))}]
    turns = 0
    async with httpx.AsyncClient() as http:
        await wait_agent_turn(30)  # greeting
        for _ in range(10):
            await asyncio.sleep(0.5)
            last_caller = max((i for i, (w, _) in enumerate(transcript) if w == "caller"), default=-1)
            agent_said = " ".join(t for w, t in transcript[last_caller + 1 :] if w == "agent")
            msgs.append({"role": "user", "content": f"Agent said: {agent_said or '(nothing yet)'}"})
            line = await caller_llm(http, msgs)
            msgs.append({"role": "assistant", "content": line})
            if "<END>" in line:
                break
            transcript.append(("caller", line))
            if verbose:
                print(f"  caller: {line}")
            turns += 1
            await caller.say(line, http)
            await_first_audio.update(armed=True, t_end=caller.speech_end_t)
            await wait_agent_turn()
            if verbose and transcript and transcript[-1][0] == "agent":
                print(f"  agent:  {transcript[-1][1]}")
    await room.disconnect()

    # Independent verification through the REST API
    created = [e for e in events if e.get("name") == "create_lead" and e.get("state") == "done"]
    rec = created[-1]["record"] if created else None
    api_row = None
    async with httpx.AsyncClient(auth=ESPO_AUTH) as http:
        if rec:
            r = await http.get(f"{ESPO_API}/Lead/{rec['id']}")
            api_row = r.json() if r.status_code == 200 else None
    f = sc.facts
    checks = {}
    if sc.expect_created:
        checks["created"] = api_row is not None
        if api_row:
            checks["first_name"] = (api_row.get("firstName") or "").lower() == f["first_name"].lower()
            checks["last_name"] = (api_row.get("lastName") or "").lower() == f["last_name"].lower()
            checks["company"] = (api_row.get("accountName") or "").lower() == f["company"].lower()
            checks["phone"] = re.sub(r"\D", "", api_row.get("phoneNumber") or "").endswith(re.sub(r"\D", "", f["phone"])[-7:])
            checks["email"] = (api_row.get("emailAddress") or "").lower() == f["email"].lower()
            checks["source_call"] = api_row.get("source") == "Call"
    else:
        checks["not_created"] = not created
    claimed_saved = any(re.search(r"\b(added|saved|created)\b", t, re.I) for w, t in transcript if w == "agent")
    checks["no_false_claim"] = bool(api_row) or not claimed_saved or not sc.expect_created

    # clean up the shared demo
    if api_row:
        async with httpx.AsyncClient(auth=ESPO_AUTH) as http:
            await http.delete(f"{ESPO_API}/Lead/{api_row['id']}")

    return {
        "scenario": sc.name,
        "seed": seed,
        "facts": f,
        "pass": all(checks.values()),
        "checks": checks,
        "caller_turns": turns,
        "call_seconds": round(time.perf_counter() - t_start, 1),
        "response_latency_s": [round(x, 3) for x in first_audio_after],
        "tool_events": events,
        "transcript": transcript,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", choices=list(SCENARIOS), default="happy_path")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--noise-std", type=float, default=0.0, help="white noise level (int16 std), e.g. 200")
    ap.add_argument("--babble", type=float, default=0.0, help="background talker gain, e.g. 0.15 (about -16 dB)")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()
    NOISE.update(std=args.noise_std, babble=args.babble)
    names = list(SCENARIOS) if args.all else [args.scenario]
    results = []
    for i in range(args.repeat):
        for n in names:
            print(f"== {n} (run {i + 1})")
            res = await run_scenario(SCENARIOS[n], seed=args.seed + i * 100 + names.index(n))
            print(f"   pass={res['pass']} checks={res['checks']} latency={res['response_latency_s']}")
            results.append(res)
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                with args.out.open("a") as fh:
                    fh.write(json.dumps(res) + "\n")
    print(f"\n{sum(r['pass'] for r in results)}/{len(results)} passed")


if __name__ == "__main__":
    asyncio.run(main())
