"""Scripted end-to-end call against the live agent: create a lead, edit it, ask a count, hang up.

The caller's lines are fixed (spoken with Deepgram TTS into the LiveKit room as a microphone track), so the run
is repeatable. The outcome is checked independently through the EspoCRM REST API, then the test lead is deleted.

  uv run python evals/e2e_call.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sys
import time
from datetime import timedelta
from pathlib import Path

import httpx
import numpy as np
from livekit import api, rtc

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from espo_voice_agent import config  # noqa: E402
from sim_call import ESPO_API, ESPO_AUTH, SR, Caller  # noqa: E402

LAST = "Kapoor"
FACTS = {"first": "Ravi", "last": LAST, "company": "Lumen Labs", "phone": "4155550188", "email": "ravi.kapoor@lumenlabs.com",
         "title": "Head of Sales"}


async def main() -> None:
    room_name = f"eval-e2e-{secrets.token_hex(3)}"
    token = (
        api.AccessToken().with_identity("sim-caller").with_ttl(timedelta(minutes=10))
        .with_grants(api.VideoGrants(room_join=True, room=room_name))
        .with_room_config(api.RoomConfiguration(agents=[api.RoomAgentDispatch(agent_name=config.AGENT_NAME)]))
        .to_jwt()
    )
    room = rtc.Room()
    log: list[tuple[float, str, str]] = []
    events: list[dict] = []
    state = {"v": "initializing", "changed": asyncio.Event()}
    lat: list[float] = []
    arm = {"on": False, "t": 0.0}
    t0 = time.perf_counter()
    caller = Caller(room)

    def say_log(who, text):
        log.append((round(time.perf_counter() - t0, 1), who, text))
        print(f"{log[-1][0]:6.1f}s  {who:6s} {text}")

    def on_attrs(_c, p):
        s = p.attributes.get("lk.agent.state")
        if s and s != state["v"]:
            state["v"] = s
            state["changed"].set()

    room.on("participant_attributes_changed", on_attrs)

    async def watch_audio(track):
        async for ev in rtc.AudioStream(track, sample_rate=SR, num_channels=1):
            if arm["on"]:
                pcm = np.frombuffer(ev.frame.data, dtype=np.int16)
                if pcm.size and np.sqrt(np.mean(pcm.astype(np.float32) ** 2)) > 300:
                    lat.append(time.perf_counter() - arm["t"])
                    arm["on"] = False

    room.on("track_subscribed", lambda t, *_: asyncio.create_task(watch_audio(t)) if t.kind == rtc.TrackKind.KIND_AUDIO else None)

    def on_text(reader, identity):
        async def _read():
            txt = await reader.read_all()
            if reader.info.topic == "agent-events":
                ev = json.loads(txt)
                events.append(ev)
                if ev.get("type") == "task" and ev.get("kind") in ("start", "question", "done", "failed"):
                    say_log("[job]", f"{ev['kind']}: {ev.get('text', '')[:110]}")
            elif identity != "sim-caller" and reader.info.attributes.get("lk.transcription_final") == "true":
                say_log("Ava", txt)

        asyncio.create_task(_read())

    room.register_text_stream_handler("lk.transcription", on_text)
    room.register_text_stream_handler("agent-events", on_text)

    async def settle(timeout=45.0):
        """Wait until Ava has spoken and is listening again (stable for 1.5 s)."""
        end, spoke = time.perf_counter() + timeout, False
        while time.perf_counter() < end:
            state["changed"].clear()
            spoke = spoke or state["v"] in ("thinking", "speaking")
            if spoke and state["v"] == "listening":
                try:
                    await asyncio.wait_for(state["changed"].wait(), 1.5)
                    continue
                except asyncio.TimeoutError:
                    return True
            try:
                await asyncio.wait_for(state["changed"].wait(), 1.0)
            except asyncio.TimeoutError:
                pass
        return False

    async def job_finished(n_before, timeout=60.0):
        end = time.perf_counter() + timeout
        while time.perf_counter() < end:
            done = [e for e in events[n_before:] if e.get("type") == "task" and e.get("kind") in ("done", "failed")]
            q = [e for e in events[n_before:] if e.get("type") == "task" and e.get("kind") == "question"]
            if done or q:
                return (done or q)[-1]
            await asyncio.sleep(0.5)
        return None

    http = httpx.AsyncClient()

    async def speak(line):
        say_log("caller", line)
        await caller.say(line, http)
        arm.update(on=True, t=caller.speech_end_t)

    def last_ava():
        return next((t for _, w, t in reversed(log) if w == "Ava"), "")

    async def confirm_job(answer="Yes, go ahead."):
        """Say yes to a read-back, then wait for the job's outcome; handle a re-ask or a browser question."""
        for _ in range(3):
            n = len(events)
            await speak(answer)
            await settle()
            res = await job_finished(n, 70)
            if res and res.get("kind") == "question":
                await settle()
                await speak("Yes, create a new one." if "exist" in res.get("text", "").lower() else "The one at Lumen Labs.")
                res = await job_finished(len(events) - 1, 70)
            if res:
                await settle(30)
                return res
            if re.search(r"(okay|shall i|correct|right)\?", last_ava(), re.I):
                answer = "Yes, that's correct. Please go ahead."
                continue
            return None
        return None

    await room.connect(os.environ["LIVEKIT_URL"], token)
    await caller.start()
    await settle(30)  # greeting

    # 1 · create
    await speak("Hi. Please add a new lead: Ravi Kapoor from Lumen Labs. Phone four one five, five five five, zero one "
                "eight eight. Email ravi dot kapoor at lumen labs dot com.")
    await settle()
    if re.search(r"spell", last_ava(), re.I):
        await speak("Ravi, R A V I. Kapoor, K A P O O R.")
        await settle()
    create = await confirm_job("Yes, add it.")

    # 2 · edit
    await speak("Great. Now change his job title to Head of Sales.")
    await settle()
    edit = await confirm_job("Yes.")

    # 3 · read-only question
    n = len(events)
    await speak("How many leads have the status New?")
    await settle()
    count = await job_finished(n, 60)
    await settle(30)

    await speak("That's all, thanks. Bye.")
    await settle(20)
    await room.disconnect()
    await http.aclose()

    # ---- independent verification
    async with httpx.AsyncClient(auth=ESPO_AUTH) as c:
        rows = (await c.get(f"{ESPO_API}/Lead", params={"where[0][type]": "equals", "where[0][attribute]": "lastName",
                                                          "where[0][value]": LAST})).json().get("list", [])
        full = [(await c.get(f"{ESPO_API}/Lead/{r['id']}")).json() for r in rows]
        n_new = (await c.get(f"{ESPO_API}/Lead", params={"where[0][type]": "equals", "where[0][attribute]": "status",
                                                          "where[0][value]": "New", "maxSize": 1})).json().get("total")
        lead = full[0] if len(full) == 1 else None
        checks = {
            "exactly one lead created": len(full) == 1,
            "name": bool(lead) and (lead.get("firstName"), lead.get("lastName")) == (FACTS["first"], FACTS["last"]),
            "company": bool(lead) and (lead.get("accountName") or "").lower() == FACTS["company"].lower(),
            "phone": bool(lead) and re.sub(r"\D", "", lead.get("phoneNumber") or "").endswith(FACTS["phone"]),
            "email": bool(lead) and (lead.get("emailAddress") or "").lower() == FACTS["email"],
            "source = Call": bool(lead) and lead.get("source") == "Call",
            "title edited": bool(lead) and (lead.get("title") or "") == FACTS["title"],
            "create job reported done": bool(create) and create.get("kind") == "done",
            "edit job reported done": bool(edit) and edit.get("kind") == "done",
            "count answer correct": bool(count) and count.get("kind") == "done"
                                     and [int(x) for x in re.findall(r"\b\d+\b", count.get("text", ""))] == [n_new],
        }
        for r in full:  # clean up the shared demo
            await c.delete(f"{ESPO_API}/Lead/{r['id']}")

    print("\nCHECKS (via EspoCRM REST API):")
    for k, v in checks.items():
        print(f"  {'PASS' if v else 'FAIL'}  {k}")
    lat_s = sorted(lat)
    print(f"\nreply latency, caller side (incl. network): n={len(lat_s)} median={lat_s[len(lat_s) // 2]:.2f}s "
          f"min={lat_s[0]:.2f}s max={lat_s[-1]:.2f}s" if lat_s else "no latency samples")
    print(f"call length {time.perf_counter() - t0:.0f}s · status-New leads per API: {n_new}")
    out = Path(__file__).parent / "results" / "e2e_call.json"
    out.write_text(json.dumps({"checks": checks, "latency_s": lat_s, "log": log, "ran_at": time.strftime("%Y-%m-%d %H:%M")}, indent=1))
    sys.exit(0 if all(checks.values()) else 1)


if __name__ == "__main__":
    asyncio.run(main())
