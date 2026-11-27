"""The browser agent's results and questions must reach the voice LLM (regression: a logging
error once swallowed every hand-off)."""

import asyncio
import json

from espo_voice_agent.agent import CrmHandle, SessionLog
from espo_voice_agent.browser_agent import BrowserTask


class FakeHandle:
    def __init__(self, interrupted=False):
        self.interrupted = interrupted

    def add_done_callback(self, cb):
        cb(self)


class FakeSession:
    def __init__(self, interrupted=False):
        self.replies = []
        self.interrupted = interrupted

    def generate_reply(self, instructions=None, **_):
        self.replies.append(instructions)
        return FakeHandle(self.interrupted)


def _crm(tmp_path, monkeypatch):
    monkeypatch.setattr("espo_voice_agent.config.LOG_DIR", tmp_path)
    crm = CrmHandle.__new__(CrmHandle)  # skip browser setup
    crm.slog = SessionLog("test")
    crm.session = FakeSession()
    crm.task = None
    crm._last_progress = ""
    crm._unannounced = None
    crm._reported = None

    async def emit(**_):
        return None

    crm.emit = emit
    return crm


def test_done_and_question_reach_voice_llm(tmp_path, monkeypatch):
    crm = _crm(tmp_path, monkeypatch)
    t = BrowserTask(id="t1", goal="set title")
    crm.task = t
    asyncio.run(crm.on_task_event(t, "progress", "opening Jane Doe"))
    asyncio.run(crm.on_task_event(t, "question", "Two Jane Does, which one?"))
    asyncio.run(crm.on_task_event(t, "done", "Set Jane Doe's title to Head of Sales."))
    assert len(crm.session.replies) == 2  # progress stays on screen only
    assert "which one" in crm.session.replies[0]
    assert "Head of Sales" in crm.session.replies[1]
    kinds = [json.loads(line).get("event") for line in crm.slog.path.read_text().splitlines()]
    assert kinds == ["progress", "question", "done"]


def test_job_context_reports_state(tmp_path, monkeypatch):
    crm = _crm(tmp_path, monkeypatch)
    assert crm.job_context() is None
    crm.task = BrowserTask(id="t1", goal="update email")
    assert "Still in progress" in crm.job_context()
    crm.task.status, crm.task.summary = "done", "Updated the email of Jane Doe."
    assert "finished and verified" in crm.job_context()
    assert crm.job_context() is None  # reported once, not on every turn


def test_interrupted_result_is_repeated_next_turn(tmp_path, monkeypatch):
    crm = _crm(tmp_path, monkeypatch)
    crm.session = FakeSession(interrupted=True)
    crm.task = BrowserTask(id="t1", goal="update phone")
    crm.task.status, crm.task.summary = "done", "Updated Jane Doe's phone."
    asyncio.run(crm.on_task_event(crm.task, "done", crm.task.summary))
    assert "may have missed" in crm.job_context()
    # regression: the reminder must fire once, not on every following turn
    assert "may have missed" not in (crm.job_context() or "")
    assert "may have missed" not in (crm.job_context() or "")
    crm.session = FakeSession(interrupted=False)
    asyncio.run(crm.on_task_event(crm.task, "done", crm.task.summary))
    assert "may have missed" not in (crm.job_context() or "")


def test_stale_task_events_do_not_reach_voice(tmp_path, monkeypatch):
    crm = _crm(tmp_path, monkeypatch)
    old, new = BrowserTask(id="t1", goal="old"), BrowserTask(id="t2", goal="new")
    crm.task = new
    asyncio.run(crm.on_task_event(old, "done", "old job finished late"))
    assert crm.session.replies == []
