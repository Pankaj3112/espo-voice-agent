"""LiveKit voice agent that adds leads to EspoCRM by driving a real browser.

Run:  uv run python -m espo_voice_agent.agent dev     (local)
      uv run python -m espo_voice_agent.agent start   (production)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

from livekit import rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    RunContext,
    cli,
    function_tool,
    room_io,
)
from livekit.plugins import deepgram, elevenlabs, noise_cancellation, openai, silero
from playwright.async_api import async_playwright

from . import config
from .espo import EspoBrowser, LeadInput
from .prompt import GREETING, INSTRUCTIONS
from .screen import BrowserStreamer

log = logging.getLogger("agent")


class SessionLog:
    """Append-only JSONL per call: transcript, per-turn latency, tool timings."""

    def __init__(self, room_name: str) -> None:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.path: Path = config.LOG_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{room_name}.jsonl"
        self.t0 = time.time()

    def write(self, kind: str, **data) -> None:
        rec = {"t": round(time.time() - self.t0, 3), "kind": kind, **data}
        with self.path.open("a") as f:
            f.write(json.dumps(rec, default=str) + "\n")


_YES = re.compile(
    r"\b(yes|yeah|yep|yup|correct|right|go ahead|sure|please do|do it|sounds good|perfect|confirm(ed)?|add (it|that|them|her|him))\b",
    re.I,
)
_NO = re.compile(r"\b(no|not|nope|wrong|incorrect|wait|hold on|change|actually|instead|but)\b|n['’]?t\b", re.I)


def is_confirmation(text: str) -> bool:
    """Deterministic guard for the one irreversible action: the caller's latest
    utterance must be an explicit yes with no correction in it."""
    return bool(_YES.search(text)) and not _NO.search(text)


class IntakeAgent(Agent):
    def __init__(self, crm: "CrmHandle") -> None:
        # Note: don't pass llm=None here; Agent treats that as "no LLM" and overrides the session's.
        super().__init__(instructions=INSTRUCTIONS)
        self.crm = crm
        self.last_user_text = ""

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        self.last_user_text = new_message.text_content or ""

    @function_tool()
    async def create_lead(
        self,
        context: RunContext,
        first_name: str,
        last_name: str,
        company: str | None = None,
        phone: str | None = None,
        email: str | None = None,
        title: str | None = None,
        notes: str | None = None,
        duplicate_confirmed: bool = False,
    ) -> str:
        """Create a new Lead in EspoCRM. Only call this after the caller has confirmed the details you read back.

        Args:
            first_name: Lead's first name, correctly capitalised.
            last_name: Lead's last name, correctly capitalised.
            company: Company / account name, if given.
            phone: Phone number exactly as dictated, digits with optional leading + and country code.
            email: Email address in normal written form, e.g. jane.doe@acme.com.
            title: Job title, if given.
            notes: Short free-text note about the lead (interest, context), if given.
            duplicate_confirmed: Set true only if you already warned the caller about a possible duplicate and they still want a new lead.
        """
        if not is_confirmation(self.last_user_text):
            return (
                "NOT CONFIRMED: the caller's last reply was not a clear yes "
                f"({self.last_user_text!r}). Nothing was saved."
            )
        lead = LeadInput(
            first_name=first_name.strip(),
            last_name=last_name.strip(),
            account_name=_clean(company),
            phone=_clean(phone),
            email=_clean(email),
            title=_clean(title),
            source="Call",
            description=_clean(notes),
        )
        return await self.crm.create(context, lead, duplicate_confirmed)

    @function_tool()
    async def find_lead(self, context: RunContext, query: str) -> str:
        """Search existing Leads in EspoCRM by name, company, email or phone. Use when the caller asks whether a lead already exists.

        Args:
            query: Text to search for, e.g. a last name or company.
        """
        return await self.crm.search(context, query)


def _clean(v: str | None) -> str | None:
    v = (v or "").strip()
    return v or None


class CrmHandle:
    """Owns the per-call browser and reports progress to the UI over a data topic."""

    def __init__(self, room: rtc.Room, slog: SessionLog) -> None:
        self.room = room
        self.slog = slog
        self.browser = EspoBrowser(config.ESPO_BASE_URL)
        self.ready = asyncio.Event()
        self.start_error: Exception | None = None
        self._pw = None
        self.streamer: BrowserStreamer | None = None

    async def start(self) -> None:
        t = time.perf_counter()
        try:
            self._pw = await async_playwright().start()
            for attempt in (1, 2):  # the shared demo occasionally times out on login
                try:
                    await self.browser.start(self._pw)
                    break
                except Exception:
                    log.warning("browser start attempt %d failed", attempt, exc_info=True)
                    await self.browser.close()
                    if attempt == 2:
                        raise
            await self.browser._goto_hash("#Lead", ".list-container")
            self.streamer = BrowserStreamer(self.browser.page, self.room, fps=config.SCREEN_FPS)
            await self.streamer.start()
            self.slog.write("browser_ready", ms=round((time.perf_counter() - t) * 1000))
        except Exception as e:  # surfaced to the caller by the tools
            log.exception("browser start failed")
            self.start_error = e
            self.slog.write("browser_failed", error=repr(e))
            await self.emit(type="tool", name="browser", state="error", detail="Could not open EspoCRM")
        finally:
            self.ready.set()

    async def close(self) -> None:
        if self.streamer:
            await self.streamer.stop()
        await self.browser.close()
        if self._pw:
            await self._pw.stop()

    async def emit(self, **event) -> None:
        try:
            await self.room.local_participant.send_text(json.dumps(event), topic="agent-events")
        except Exception:
            log.warning("could not send UI event", exc_info=True)

    async def _wait_ready(self) -> str | None:
        await asyncio.wait_for(self.ready.wait(), timeout=30)
        if self.start_error:
            return "The CRM browser failed to start, so nothing was saved. Apologise and suggest trying again shortly."
        return None

    async def create(self, context: RunContext, lead: LeadInput, duplicate_confirmed: bool) -> str:
        # Speak while the browser works so the caller is never left in silence.
        context.session.say("Okay, one moment while I enter that in the CRM.", add_to_chat_ctx=False)
        if err := await self._wait_ready():
            return err
        t = time.perf_counter()
        try:
            if not duplicate_confirmed:
                await self.emit(type="tool", name="duplicate_check", state="start")
                dup = await self.browser.find_duplicates(lead)
                self.slog.write("tool", name="find_duplicates", ok=dup.ok, n=len(dup.matches), timings=dup.timings_ms)
                await self.emit(type="tool", name="duplicate_check", state="done", detail=dup.message)
                if dup.matches:
                    m = dup.matches[0]
                    return (
                        "NOT CREATED: possible duplicate already in the CRM: "
                        f"{m['name']} ({m['accountName'] or 'no company'}, {m['emailAddress'] or 'no email'}, "
                        f"{m['phoneNumber'] or 'no phone'}), created {m['createdAt']}. "
                        "Tell the caller and ask whether to create a new lead anyway."
                    )
            await self.emit(type="tool", name="create_lead", state="start", detail=f"{lead.first_name} {lead.last_name}")
            res = await self.browser.create_lead(lead, allow_duplicate=duplicate_confirmed)
        except Exception as e:
            log.exception("create_lead failed")
            self.slog.write("tool", name="create_lead", ok=False, error=repr(e))
            await self.emit(type="tool", name="create_lead", state="error", detail=str(e)[:200])
            return "FAILED: the CRM did not respond as expected and the lead may not be saved. Tell the caller honestly."
        total_ms = round((time.perf_counter() - t) * 1000)
        self.slog.write("tool", name="create_lead", ok=res.ok, msg=res.message, timings=res.timings_ms, total_ms=total_ms)
        rec = res.record
        await self.emit(
            type="tool",
            name="create_lead",
            state="done" if res.ok else "error",
            detail=res.message,
            url=rec.url if rec else None,
            record=rec.__dict__ if rec else None,
        )
        if res.ok and rec:
            return (
                f"CREATED and verified on screen: {rec.name}, company {rec.account_name or '-'}, "
                f"phone {rec.phone or '-'}, email {rec.email or '-'}, status {rec.status}."
            )
        return f"PROBLEM: {res.message}. Tell the caller what went wrong."

    async def search(self, context: RunContext, query: str) -> str:
        context.session.say("Let me look that up.", add_to_chat_ctx=False)
        if err := await self._wait_ready():
            return err
        await self.emit(type="tool", name="find_lead", state="start", detail=query)
        res = await self.browser.search_leads(query)
        self.slog.write("tool", name="find_lead", ok=res.ok, n=len(res.matches), timings=res.timings_ms)
        await self.emit(type="tool", name="find_lead", state="done", detail=res.message)
        if not res.matches:
            return f"No leads match '{query}'."
        rows = "; ".join(f"{m['name']} ({m['emailAddress'] or 'no email'}, {m['phoneNumber'] or 'no phone'}, {m['status']})" for m in res.matches)
        return f"{len(res.matches)} match(es): {rows}"


def build_llm():
    if config.LLM_BACKEND == "vertex":
        from livekit.plugins import google

        # Credentials: the VM's service account (Application Default Credentials).
        return google.LLM(
            model=config.LLM_MODEL,
            vertexai=True,
            project=config.GCP_PROJECT,
            location=config.GCP_LOCATION,
            temperature=0.2,
            thinking_config={"thinking_level": config.LLM_REASONING},
        )
    return openai.LLM.with_openrouter(
        model=config.LLM_MODEL,
        provider={"order": [config.LLM_PROVIDER], "allow_fallbacks": True},
        reasoning_effort=config.LLM_REASONING,
        temperature=0.2,
        app_name="espo-voice-agent",
    )


def build_tts():
    if config.TTS_PROVIDER == "deepgram":
        return deepgram.TTS(model="aura-2-andromeda-en")
    return elevenlabs.TTS(voice_id=config.TTS_VOICE_ID, model=config.TTS_MODEL)


server = AgentServer(num_idle_processes=1)


def prewarm(proc: JobProcess) -> None:
    proc.userdata["vad"] = silero.VAD.load()


server.setup_fnc = prewarm


# LiveKit's adaptive (model-based) barge-in detection tells real interruptions from noise, echo and
# "mm-hm". It is on by default only in dev mode, so production must ask for it explicitly; otherwise
# any VAD blip pauses the agent's reply until the 2 s false-interruption timeout.
INTERRUPTION = {"mode": "adaptive", "min_duration": 0.5, "resume_false_interruption": True, "false_interruption_timeout": 1.0}


def build_stt_and_turns():
    keyterms = ["EspoCRM", "lead"]
    if config.STT_MODE == "flux":
        # Flux emits end-of-turn itself; the eager EOT lets the LLM start before the turn is final.
        stt = deepgram.STTv2(
            model=config.STT_MODEL,
            eot_threshold=config.FLUX_EOT_THRESHOLD,
            eager_eot_threshold=config.FLUX_EAGER_EOT_THRESHOLD,
            eot_timeout_ms=config.FLUX_EOT_TIMEOUT_MS,
            keyterm=keyterms,
        )
        return stt, {"turn_detection": "stt", "endpointing": {"min_delay": 0.0, "max_delay": 2.0}, "interruption": INTERRUPTION}
    from livekit.plugins.turn_detector.multilingual import MultilingualModel

    stt = deepgram.STT(model=config.STT_MODEL, keyterm=keyterms, smart_format=True)
    return stt, {"turn_detection": MultilingualModel(), "endpointing": {"min_delay": 0.4, "max_delay": 2.0}, "interruption": INTERRUPTION}


@server.rtc_session(agent_name=config.AGENT_NAME)
async def entrypoint(ctx: JobContext) -> None:
    slog = SessionLog(ctx.room.name)
    await ctx.connect()
    crm = CrmHandle(ctx.room, slog)
    browser_task = asyncio.create_task(crm.start())

    stt, turn_handling = build_stt_and_turns()
    session = AgentSession(
        vad=ctx.proc.userdata["vad"],
        stt=stt,
        llm=build_llm(),
        tts=build_tts(),
        turn_handling=turn_handling,
    )

    last_user_stop: dict[str, float | None] = {"t": None}

    @session.on("conversation_item_added")
    def _on_item(ev) -> None:
        item = ev.item
        role = getattr(item, "role", None)
        if role not in ("user", "assistant"):
            return
        m = dict(getattr(item, "metrics", {}) or {})
        rec = {"role": role, "text": item.text_content, "metrics": m}
        if role == "user":
            last_user_stop["t"] = m.get("stopped_speaking_at")
        elif last_user_stop["t"] and m.get("started_speaking_at"):
            # what the caller feels: end of their speech -> first agent audio
            rec["response_latency_s"] = round(m["started_speaking_at"] - last_user_stop["t"], 3)
            last_user_stop["t"] = None
        slog.write("turn", **rec)

    # Fine-grained timeline (wall clock) to see where each second of a reply goes.
    @session.on("user_state_changed")
    def _on_user_state(ev) -> None:
        slog.write("user_state", state=ev.new_state, at=time.time())

    @session.on("agent_state_changed")
    def _on_agent_state(ev) -> None:
        slog.write("agent_state", state=ev.new_state, at=time.time())

    @session.on("user_input_transcribed")
    def _on_transcript(ev) -> None:
        if ev.is_final:
            slog.write("stt_final", text=ev.transcript[:80], at=time.time())

    @session.on("metrics_collected")
    def _on_metrics(ev) -> None:
        m = ev.metrics
        d = {k: v for k, v in (m.model_dump() if hasattr(m, "model_dump") else {}).items()
             if isinstance(v, (int, float, str, bool)) and k not in ("label", "request_id", "speech_id")}
        slog.write("metrics", mtype=type(m).__name__, at=time.time(), **d)

    async def _shutdown() -> None:
        await crm.close()
        slog.write("end")

    ctx.add_shutdown_callback(_shutdown)

    await session.start(
        agent=IntakeAgent(crm),
        room=ctx.room,
        # Krisp-based background voice cancellation (LiveKit Cloud): strips room noise and other
        # voices before VAD/STT, so they don't count as the caller speaking.
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(noise_cancellation=noise_cancellation.BVC()),
        ),
    )
    slog.write(
        "session_started", room=ctx.room.name, llm=config.LLM_MODEL, stt=config.STT_MODEL, stt_mode=config.STT_MODE,
        tts=f"{config.TTS_PROVIDER}:{config.TTS_MODEL if config.TTS_PROVIDER == 'elevenlabs' else 'aura-2'}",
    )
    session.say(GREETING)

    async def _time_limit() -> None:
        await asyncio.sleep(config.MAX_CALL_SECONDS)
        slog.write("time_limit")
        await session.say("We've reached the time limit for this demo call. Thanks for trying it, goodbye!")
        ctx.shutdown(reason="time limit")

    asyncio.create_task(_time_limit())
    await browser_task


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cli.run_app(server)
