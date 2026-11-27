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
from .browser_agent import BrowserAgent, BrowserTask
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
        # Ground every reply in the real state of the CRM job, so the LLM never has to guess
        # whether something finished (results are also pushed to it as soon as they arrive).
        if self.crm is not None and (status := self.crm.job_context()):
            turn_ctx.add_message(role="assistant", content=status)

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
        """Create a NEW Lead in EspoCRM (fast, scripted). Only call this after the caller has confirmed the details you read back.

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
        return await self.crm.create(lead, duplicate_confirmed)

    @function_tool()
    async def browser_task(self, context: RunContext, instructions: str, changes_data: bool) -> str:
        """Hand a job to the browser agent, which operates the EspoCRM web app like a person (open records, edit
        fields, change status, add notes, look things up). It runs in the background; you keep talking. Its result,
        questions and progress come back to you automatically.

        Args:
            instructions: Plain-language task with every value needed, e.g. "Set the phone of lead Jane Doe at ACME to 1112131415".
            changes_data: True if the task edits, adds or deletes anything (then the caller must have just confirmed it).
        """
        if changes_data and not is_confirmation(self.last_user_text):
            return (
                "NOT CONFIRMED: read the change back to the caller and get a clear yes first "
                f"(their last reply was {self.last_user_text!r}). Nothing was started."
            )
        return await self.crm.start_browser_task(instructions)

    @function_tool()
    async def message_browser_agent(self, context: RunContext, message: str) -> str:
        """Send the running browser task the caller's answer to its question, or extra details/corrections.

        Args:
            message: What to pass on, in plain language.
        """
        return await self.crm.message_task(message)

    @function_tool()
    async def browser_task_status(self, context: RunContext) -> str:
        """What the browser agent is doing right now (use if the caller asks how it's going)."""
        return self.crm.task_status()

    @function_tool()
    async def cancel_browser_task(self, context: RunContext) -> str:
        """Stop the running browser task (if the caller changes their mind)."""
        return await self.crm.cancel_task()

    @function_tool()
    async def find_lead(self, context: RunContext, query: str) -> str:
        """Quick search of existing Leads by name, company, email or phone (returns name, email, phone, status).

        Args:
            query: Text to search for, e.g. a last name or company.
        """
        return await self.crm.search(query)


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
        self.session: AgentSession | None = None
        self.agent: BrowserAgent | None = None
        self.task: BrowserTask | None = None
        self._last_progress = ""
        self._heartbeat: asyncio.Task | None = None

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
        if self.agent:
            await self.agent.cancel()
        if self._heartbeat:
            self._heartbeat.cancel()
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

    # ---- background work -------------------------------------------------------------------
    # Browser work never blocks the conversation: tools start it and return at once, the voice
    # LLM says something natural in its own words, and the outcome is handed back to it later.

    def busy(self) -> str | None:
        t = self.task
        if t and t.status in ("running", "waiting_for_caller"):
            return f"BUSY: the browser is still working on: {t.goal!r} ({t.status}). Wait for it, or cancel it first."
        return None

    async def create(self, lead: LeadInput, duplicate_confirmed: bool) -> str:
        if err := await self._wait_ready():
            return err
        if b := self.busy():
            return b
        if not duplicate_confirmed:  # fast (~0.1 s), so it stays synchronous
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
        self.task = BrowserTask(id=f"c{int(time.time())}", goal=f"create lead {lead.first_name} {lead.last_name}")
        self.task._runner = asyncio.create_task(self._create_bg(self.task, lead, duplicate_confirmed))  # type: ignore[attr-defined]
        return (
            "STARTED: the lead is being entered in the CRM now (takes a few seconds). Tell the caller briefly, in your "
            "own words, that you're adding it. Do not say it is added until the result arrives."
        )

    async def _create_bg(self, task: BrowserTask, lead: LeadInput, duplicate_confirmed: bool) -> None:
        text = await self._create(lead, duplicate_confirmed)
        task.status = "done" if text.startswith("CREATED") else "failed"
        task.summary = text
        await self.on_task_event(task, task.status, text)

    async def _create(self, lead: LeadInput, duplicate_confirmed: bool) -> str:
        t = time.perf_counter()
        try:
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
                "CREATED a NEW, separate lead record (no existing record was changed), verified on screen: "
                f"{rec.name}, company {rec.account_name or '-'}, "
                f"phone {rec.phone or '-'}, email {rec.email or '-'}, status {rec.status}."
            )
        return f"PROBLEM: {res.message}. Tell the caller what went wrong."

    async def search(self, query: str) -> str:
        if err := await self._wait_ready():
            return err
        if b := self.busy():
            return b
        await self.emit(type="tool", name="find_lead", state="start", detail=query)
        res = await self.browser.search_leads(query)
        self.slog.write("tool", name="find_lead", ok=res.ok, n=len(res.matches), timings=res.timings_ms)
        await self.emit(type="tool", name="find_lead", state="done", detail=res.message)
        if not res.matches:
            return f"No leads match '{query}'."
        rows = "; ".join(f"{m['name']} ({m['emailAddress'] or 'no email'}, {m['phoneNumber'] or 'no phone'}, {m['status']})" for m in res.matches)
        return f"{len(res.matches)} match(es): {rows}"


    async def start_browser_task(self, instructions: str) -> str:
        if err := await self._wait_ready():
            return err
        if b := self.busy():
            return b
        if self.agent is None:
            self.agent = BrowserAgent(self.browser.page, self.on_task_event, self.browser._lock)
        self.task = self.agent.start(instructions)
        self.slog.write("browser_task", id=self.task.id, goal=instructions)
        await self.emit(type="task", id=self.task.id, kind="start", text=instructions)
        self._heartbeat = asyncio.create_task(self._heartbeat_loop(self.task))
        return (
            f"STARTED browser task {self.task.id}. Tell the caller briefly, in your own words, that you're on it. "
            "You'll receive its progress, questions and result automatically; never claim it's done before that."
        )

    async def message_task(self, message: str) -> str:
        t = self.task
        if not t or t.status not in ("running", "waiting_for_caller"):
            return "No browser task is running."
        t.inbox.put_nowait(message)
        await self.emit(type="task", id=t.id, kind="message", text=message)
        return "Passed on to the browser agent."

    def job_context(self) -> str | None:
        t = self.task
        if not t:
            return None
        if t.status in ("running", "waiting_for_caller"):
            q = f" It is waiting for the caller to answer: {t.pending_question!r}." if t.pending_question else ""
            return f"[CRM job status] Still in progress: {t.goal!r}.{q} Nothing about it is confirmed yet."
        if t.status == "done":
            return f"[CRM job status] Finished and verified: {t.summary}"
        return f"[CRM job status] Did not complete ({t.status}): {t.summary}"

    def task_status(self) -> str:
        t = self.task
        if not t:
            return "No browser task has run in this call."
        return f"Task {t.id} ({t.goal!r}) is {t.status}; recent steps: {t.recent(4) or 'starting'}."

    async def cancel_task(self) -> str:
        if self.agent and self.task and self.task.status in ("running", "waiting_for_caller"):
            await self.agent.cancel()
            return "Cancelled. Tell the caller; anything already saved stays saved."
        return "Nothing is running."

    async def on_task_event(self, task: BrowserTask, kind: str, text: str) -> None:
        """Progress goes to the screen; questions and results go to the voice LLM, which phrases them."""
        self.slog.write("task_event", id=task.id, event=kind, text=text)
        await self.emit(type="task", id=task.id, kind=kind, text=text)
        if kind in ("step", "progress"):
            self._last_progress = text
            return
        if kind in ("answer", "message"):
            return
        if self.session is None:
            return
        if kind == "question":
            instr = (
                f"The browser agent needs an answer to continue: {text!r}. Ask the caller this in your own words, then "
                "pass their answer on with message_browser_agent."
            )
        elif kind == "done":
            instr = (
                f"Browser result (verified on screen): {text} Tell the caller in one short sentence, in your own words, "
                "stating only what this says."
            )
        else:
            instr = (
                f"Browser task did NOT complete: {text} Tell the caller honestly and briefly; don't claim anything was saved "
                "unless this says so."
            )
        self.session.generate_reply(instructions=instr)

    async def _heartbeat_loop(self, task: BrowserTask) -> None:
        """If a task runs long and the line is quiet, let the voice LLM give a short natural update."""
        try:
            await asyncio.sleep(8)
            while task.status == "running":
                s = self.session
                if s and s.agent_state == "listening" and s.user_state != "speaking":
                    s.generate_reply(
                        instructions=(
                            f"The browser task is still running (latest step: {self._last_progress or 'working'}). "
                            "Give the caller a very short, natural progress update; don't repeat earlier wording."
                        )
                    )
                await asyncio.sleep(12)
        except asyncio.CancelledError:
            pass


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
    if config.TTS_PROVIDER == "fish":
        from livekit.agents import inference
        from livekit.agents import tts as lktts

        # If Fish errors or LiveKit's inference allowance runs out, keep talking in the backup voice.
        return lktts.FallbackAdapter(
            [inference.TTS(model=config.TTS_MODEL), deepgram.TTS(model=config.TTS_FALLBACK_MODEL)]
        )
    if config.TTS_PROVIDER == "deepgram":
        return deepgram.TTS(model=config.TTS_MODEL)
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

    crm.session = session
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
        tts=f"{config.TTS_PROVIDER}:{config.TTS_MODEL}",
    )
    session.say(GREETING)

    async def _time_limit() -> None:
        await asyncio.sleep(config.MAX_CALL_SECONDS)
        slog.write("time_limit")
        await session.say("We've reached the time limit for this demo call. Thanks for trying it, goodbye!")
        ctx.shutdown(reason="time limit")

    asyncio.create_task(_time_limit())

    @ctx.room.on("participant_disconnected")
    def _caller_left(p: rtc.RemoteParticipant) -> None:
        # End the job (browser, session, room seat) as soon as the caller hangs up.
        if p.kind != rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
            slog.write("caller_left")
            ctx.shutdown(reason="caller left")
    await browser_task


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    cli.run_app(server)
