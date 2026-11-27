"""Browser agent: a second LLM that operates the CRM web UI with generic page tools.

The voice agent stays on the conversation and hands work over in plain language
("add the phone 415 555 0199 to Jane Doe at ACME"). This agent then looks at the
page (a compact outline of visible text and controls, each control with a ref),
clicks, types and navigates until the job is done, and verifies the result on
screen before it reports back. While it runs it posts short progress notes, can
ask the caller a question through the voice agent, and accepts follow-up
messages from the voice agent.

Nothing here is specific to EspoCRM except the start URL: the tools only see the
rendered page, so the same loop works on other web apps.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urljoin, urlparse

from google import genai
from google.genai import types
from playwright.async_api import Page

from . import config

log = logging.getLogger("browser-agent")

# Compact, generic page outline. Every visible control gets a ref (data-agent-ref)
# that the model passes back to click/type. Text blocks give context (labels,
# field values, table cells, messages).
_SNAPSHOT_JS = r"""
(maxLines) => {
  document.querySelectorAll('[data-agent-ref]').forEach(e => e.removeAttribute('data-agent-ref'));
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && Number(s.opacity) > 0.05;
  };
  const clean = (t, n) => (t || '').replace(/\s+/g, ' ').trim().slice(0, n);
  const isCtl = (el) => el.matches('a[href],button,input,select,textarea,[role=button],[role=link],[role=menuitem],[role=option],[role=tab],[role=checkbox],[contenteditable=true],.selectize-input,.selectize-dropdown .option,[data-action]');
  const nameOf = (el) => {
    let n = clean(el.innerText, 80) || el.getAttribute('aria-label') || '';
    if (!n && el.id) { const l = document.querySelector(`label[for="${el.id}"]`); if (l) n = l.innerText; }
    if (!n) n = el.getAttribute('placeholder') || el.getAttribute('title') || (el.tagName === 'INPUT' ? '' : el.value) || '';
    if (!n) { const cell = el.closest('[data-name]'); if (cell) n = cell.getAttribute('data-name'); }
    return clean(n, 80);
  };
  const lines = []; let ref = 0;
  const walk = (el, depth) => {
    if (lines.length >= maxLines || !(el instanceof Element)) return;
    if (['SCRIPT','STYLE','NOSCRIPT','SVG','TEMPLATE'].includes(el.tagName)) return;
    if (!vis(el)) return;
    if (/^H[1-4]$/.test(el.tagName)) { const t = clean(el.innerText, 120); if (t) lines.push(`# ${t}`); return; }
    if (isCtl(el)) {
      ref += 1; el.setAttribute('data-agent-ref', String(ref));
      const tag = el.tagName.toLowerCase();
      const type = el.getAttribute('type') || '';
      const role = el.getAttribute('role') || (tag === 'a' ? 'link' : tag === 'select' ? 'select' : tag === 'textarea' ? 'textarea' : tag === 'input' ? `input${type ? ':' + type : ''}` : el.classList.contains('selectize-input') ? 'dropdown' : el.classList.contains('option') ? 'option' : 'button');
      const own = el.getAttribute('data-name') || el.getAttribute('data-action') || el.getAttribute('name');
      const field = el.parentElement?.closest('[data-name]')?.getAttribute('data-name');
      let extra = own ? ` id=${own}` : '';
      if (tag === 'input' || tag === 'textarea') extra += ` value="${clean(el.value, 80)}"` + (el.getAttribute('placeholder') ? ` placeholder="${clean(el.getAttribute('placeholder'), 40)}"` : '');
      if (tag === 'select') extra += ` value="${clean(el.options[el.selectedIndex]?.text, 40)}" options=[${[...el.options].slice(0, 12).map(o => clean(o.text, 25)).join('|')}]`;
      lines.push(`[${ref}] ${role} "${nameOf(el)}"${field ? ` field=${field}` : ''}${extra}`);
      if (tag !== 'a' && tag !== 'button') return;
      return;
    }
    // leaf-ish text: element with its own text and no element children carrying text
    const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(' ');
    const t = clean(own, 160);
    if (t && t.length > 1 && lines[lines.length - 1] !== t) {
      const field = el.closest('[data-name]')?.getAttribute('data-name');
      lines.push(field && !el.closest('label') ? `${field}: ${t}` : t);
    }
    for (const c of el.children) walk(c, depth + 1);
  };
  walk(document.body, 0);
  const modal = document.querySelector('.modal-dialog');
  return {url: location.href, title: document.title, modal: !!(modal && vis(modal)), lines};
}
"""

_SYSTEM = """\
You operate a CRM web app (EspoCRM) in a real browser on behalf of a voice assistant that is on a live \
call with a sales rep. You receive a task in plain language and complete it through the UI.

How you see the page: each tool returns a compact outline of the visible page. Controls appear as \
`[ref] role "name" field=... value=...`; text lines give labels, field values and messages. Use the refs \
with click / type_text / select_option. Refs change after every action, so always use the latest outline.

Skills (fast, scripted, verified; prefer them when they fit):
- create_lead: adds a NEW lead by filling and saving the form, then checks the saved record. It first checks for \
duplicates: if it reports a DUPLICATE, ask the caller (ask_caller) whether to create a new lead anyway, and only \
then call it again with allow_duplicate=true. Never use it to change an existing lead.
- search_leads: finds leads by name, company, email or phone and returns their ids; then open one with \
go_to("#Lead/view/<id>") instead of clicking through the list.
For anything else use the generic page tools.

Work efficiently: prefer direct navigation (e.g. go_to("#Lead/view/<id>") after search_leads, or go_to("#Lead")) \
over exploring. Typical edit flow in EspoCRM: open the record, click Edit, change \
the field, click Save, then read the detail view. For a dropdown, click it, then click the wanted `option` in \
the next outline.

Rules:
- Never invent data. Only enter values that are in the task or in messages from the voice assistant.
- If the task is ambiguous (e.g. two records match) or a needed value is missing, call ask_caller with one \
short question and wait for the answer. Do not guess.
- Do not delete anything unless the task explicitly says to delete.
- After saving, check the saved values on the record's detail view before finishing.
- Call report_progress with a few plain words when you start a meaningful step (e.g. "opening Jane Doe", \
"saving the new phone"). Keep it occasional, not every click.
- Finish with finish(success, summary). The summary is what the assistant will tell the caller: one short \
sentence stating only what you verified on screen, including the record name. If something failed, say \
what and why.
"""


def _decl(name: str, desc: str, props: dict | None = None, required: list[str] | None = None) -> types.FunctionDeclaration:
    return types.FunctionDeclaration(
        name=name,
        description=desc,
        parameters=types.Schema(type="OBJECT", properties=props or {}, required=required or []) if props else None,
    )


_S = lambda d: types.Schema(type="STRING", description=d)  # noqa: E731
_I = lambda d: types.Schema(type="INTEGER", description=d)  # noqa: E731
_B = lambda d: types.Schema(type="BOOLEAN", description=d)  # noqa: E731

TOOLS = types.Tool(
    function_declarations=[
        _decl(
            "create_lead",
            "SKILL: create a NEW lead via the scripted form fill (fast, verified). Checks for duplicates first.",
            {
                "first_name": _S("first name"), "last_name": _S("last name"), "company": _S("company / account name"),
                "phone": _S("phone digits, optional leading +"), "email": _S("email address"), "title": _S("job title"),
                "description": _S("short note"), "allow_duplicate": _B("true only after the caller agreed to add it despite a duplicate"),
            },
            ["first_name", "last_name"],
        ),
        _decl("search_leads", "SKILL: search leads; returns id, name, company, email, phone, status for each match.", {"query": _S("name, company, email or phone")}, ["query"]),
        _decl("look", "Return the current page outline (use if unsure what is on screen)."),
        _decl("click", "Click a control.", {"ref": _I("ref from the latest outline")}, ["ref"]),
        _decl(
            "type_text",
            "Replace the text in an input/textarea, typing it visibly. Set submit=true to press Enter afterwards.",
            {"ref": _I("ref of the input"), "text": _S("text to type"), "submit": _B("press Enter after typing")},
            ["ref", "text"],
        ),
        _decl("select_option", "Choose an option in a native select.", {"ref": _I("ref"), "option": _S("visible option text")}, ["ref", "option"]),
        _decl("press_key", "Press a key on the focused element, e.g. Enter, Escape, Tab.", {"key": _S("key name")}, ["key"]),
        _decl("go_to", "Navigate within the CRM: a hash route like '#Lead' or '#Lead/view/<id>', or a path.", {"target": _S("hash or path")}, ["target"]),
        _decl("scroll", "Scroll the page.", {"direction": _S("down or up")}, ["direction"]),
        _decl("report_progress", "Tell the voice assistant (and the caller's screen) what you are doing now.", {"message": _S("a few plain words")}, ["message"]),
        _decl(
            "ask_caller",
            "Ask the caller a question through the voice assistant and wait for the answer (blocks until answered).",
            {"question": _S("one short question")},
            ["question"],
        ),
        _decl(
            "finish",
            "End the task.",
            {"success": _B("true only if the result was verified on screen"), "summary": _S("one sentence for the caller, only verified facts")},
            ["success", "summary"],
        ),
    ]
)


@dataclass
class TaskEvent:
    t: float
    kind: str  # step | progress | question | answer | message | done | failed
    text: str


@dataclass
class BrowserTask:
    id: str
    goal: str
    status: str = "running"  # running | waiting_for_caller | done | failed | cancelled
    summary: str = ""
    events: list[TaskEvent] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    inbox: asyncio.Queue = field(default_factory=asyncio.Queue)
    pending_question: str | None = None
    steps: int = 0
    llm_ms: list[int] = field(default_factory=list)

    def recent(self, n: int = 5) -> str:
        return "; ".join(f"{e.kind}: {e.text}" for e in self.events[-n:])


Notify = Callable[[BrowserTask, str, str], Awaitable[None]]  # (task, kind, text)


class PageTools:
    """Generic browser actions on one Playwright page, with the active element highlighted."""

    def __init__(self, page: Page, base_url: str) -> None:
        self.page = page
        self.base = base_url.rstrip("/") + "/"

    async def outline(self, max_lines: int = 220) -> str:
        await self.page.wait_for_timeout(250)  # let the SPA settle after an action
        snap = await self.page.evaluate(_SNAPSHOT_JS, max_lines)
        head = f"URL: {snap['url']}\nTITLE: {snap['title']}" + ("\nA MODAL DIALOG IS OPEN" if snap["modal"] else "")
        return head + "\n" + "\n".join(snap["lines"])

    def _loc(self, ref: int):
        return self.page.locator(f'[data-agent-ref="{int(ref)}"]').first

    async def _highlight(self, ref: int) -> None:
        await self.page.evaluate(
            """(r) => { document.querySelectorAll('.va-focus').forEach(e => e.classList.remove('va-focus'));
                       const el = document.querySelector(`[data-agent-ref="${r}"]`);
                       if (el) { el.classList.add('va-focus'); el.scrollIntoView({block: 'center', behavior: 'instant'}); } }""",
            int(ref),
        )

    async def click(self, ref: int) -> str:
        await self._highlight(ref)
        await self._loc(ref).click(timeout=5000)
        return "clicked"

    async def type_text(self, ref: int, text: str, submit: bool = False) -> str:
        await self._highlight(ref)
        loc = self._loc(ref)
        await loc.click(timeout=5000)
        await loc.fill("")
        await loc.press_sequentially(text, delay=12)
        if submit:
            await loc.press("Enter")
        return "typed"

    async def select_option(self, ref: int, option: str) -> str:
        await self._highlight(ref)
        await self._loc(ref).select_option(label=option, timeout=5000)
        return "selected"

    async def press_key(self, key: str) -> str:
        await self.page.keyboard.press(key)
        return "pressed"

    async def go_to(self, target: str) -> str:
        if target.startswith("#"):
            await self.page.evaluate("(h) => { location.hash = h; }", target)
            await self.page.wait_for_timeout(600)
            return "navigated"
        url = urljoin(self.base, target)
        if urlparse(url).netloc != urlparse(self.base).netloc:
            return "refused: only pages inside the CRM are allowed"
        await self.page.goto(url)
        return "navigated"

    async def scroll(self, direction: str) -> str:
        await self.page.mouse.wheel(0, 600 if direction != "up" else -600)
        return "scrolled"


class BrowserAgent:
    """Runs one task at a time on the shared page (the caller watches the same page)."""

    def __init__(self, page: Page, notify: Notify, lock: asyncio.Lock, model: str | None = None, skills=None) -> None:
        self.tools = PageTools(page, config.ESPO_BASE_URL)
        self.notify = notify
        self.lock = lock  # serialises tasks (not the EspoBrowser lock, which the skills take themselves)
        self.skills = skills  # EspoBrowser: scripted create/search routines
        self.model = model or config.BROWSER_LLM_MODEL
        self.client = genai.Client(vertexai=True, project=config.GCP_PROJECT, location=config.GCP_LOCATION)
        self.current: BrowserTask | None = None
        self._n = 0

    def start(self, goal: str) -> BrowserTask:
        self._n += 1
        task = BrowserTask(id=f"t{self._n}", goal=goal)
        self.current = task
        task._runner = asyncio.create_task(self._run(task))  # type: ignore[attr-defined]
        return task

    async def cancel(self) -> None:
        t = self.current
        if t and t.status in ("running", "waiting_for_caller"):
            t.status = "cancelled"
            t._runner.cancel()  # type: ignore[attr-defined]

    async def _event(self, task: BrowserTask, kind: str, text: str) -> None:
        task.events.append(TaskEvent(time.time() - task.started, kind, text))
        try:
            await self.notify(task, kind, text)
        except Exception:
            log.warning("notify failed", exc_info=True)

    async def _run(self, task: BrowserTask) -> None:
        try:
            async with self.lock:
                await asyncio.wait_for(self._loop(task), timeout=config.BROWSER_TASK_TIMEOUT_S)
        except asyncio.TimeoutError:
            task.status, task.summary = "failed", "The browser task took too long and was stopped; it may be incomplete."
            await self._event(task, "failed", task.summary)
        except asyncio.CancelledError:
            task.status = "cancelled"
            await self._event(task, "failed", "cancelled")
        except Exception as e:
            log.exception("browser task crashed")
            task.status, task.summary = "failed", f"The browser task hit an error: {e!s:.150}"
            await self._event(task, "failed", task.summary)

    async def _loop(self, task: BrowserTask) -> None:
        outline = await self.tools.outline()
        contents: list[types.Content] = [
            types.Content(role="user", parts=[types.Part(text=f"TASK: {task.goal}\n\nCURRENT PAGE:\n{outline}")])
        ]
        cfg = types.GenerateContentConfig(
            system_instruction=_SYSTEM,
            tools=[TOOLS],
            temperature=0.1,
            thinking_config=types.ThinkingConfig(thinking_level=config.BROWSER_LLM_THINKING),
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        for _ in range(config.BROWSER_TASK_MAX_STEPS):
            # messages the voice agent sent mid-task are appended as user turns
            while not task.inbox.empty():
                msg = task.inbox.get_nowait()
                contents.append(types.Content(role="user", parts=[types.Part(text=f"MESSAGE FROM THE VOICE ASSISTANT: {msg}")]))
            t0 = time.perf_counter()
            resp = await self.client.aio.models.generate_content(model=self.model, contents=contents, config=cfg)
            task.llm_ms.append(round((time.perf_counter() - t0) * 1000))
            cand = resp.candidates[0].content if resp.candidates else None
            if cand is None or not cand.parts:
                contents.append(types.Content(role="user", parts=[types.Part(text="Continue: call a tool, or finish().")]))
                continue
            contents.append(cand)
            calls = [p.function_call for p in cand.parts if p.function_call]
            if not calls:
                contents.append(types.Content(role="user", parts=[types.Part(text="Use the tools; call finish() when done.")]))
                continue
            replies: list[types.Part] = []
            for call in calls:
                task.steps += 1
                result = await self._exec(task, call.name, dict(call.args or {}))
                if task.status in ("done", "failed"):
                    return
                replies.append(types.Part(function_response=types.FunctionResponse(name=call.name, response={"result": result})))
            contents.append(types.Content(role="user", parts=replies))
        task.status, task.summary = "failed", "The browser task ran out of steps before finishing; it may be incomplete."
        await self._event(task, "failed", task.summary)

    async def _skill(self, task: BrowserTask, name: str, args: dict[str, Any]) -> str:
        from .espo import LeadInput  # local import keeps this module free of EspoCRM at import time

        if self.skills is None:
            return "This skill is not available; use the page tools."
        b = self.skills
        if name == "search_leads":
            await self._event(task, "progress", f"searching leads for {args.get('query', '')}"[:120])
            res = await b.search_leads(str(args.get("query", "")))
            rows = [{k: m.get(k) for k in ("id", "name", "accountName", "emailAddress", "phoneNumber", "status")} for m in res.matches]
            return f"{len(rows)} match(es): {json.dumps(rows)}" if rows else "No leads match."
        clean = lambda v: (str(v).strip() or None) if v is not None else None  # noqa: E731
        lead = LeadInput(
            first_name=str(args.get("first_name", "")).strip(), last_name=str(args.get("last_name", "")).strip(),
            account_name=clean(args.get("company")), phone=clean(args.get("phone")), email=clean(args.get("email")),
            title=clean(args.get("title")), source="Call", description=clean(args.get("description")),
        )
        allow = bool(args.get("allow_duplicate"))
        if not allow:
            dup = await b.find_duplicates(lead)
            if dup.matches:
                m = dup.matches[0]
                return (
                    f"DUPLICATE: {m['name']} ({m.get('accountName') or 'no company'}, {m.get('emailAddress') or 'no email'}, "
                    f"{m.get('phoneNumber') or 'no phone'}) already exists, id {m.get('id')}. Nothing was created. Ask the caller."
                )
        await self._event(task, "progress", f"filling in the new lead {lead.first_name} {lead.last_name}")
        res = await b.create_lead(lead, allow_duplicate=allow)
        rec = res.record
        if rec:
            await self._event(task, "record", json.dumps(rec.__dict__))
        if res.ok and rec:
            return (f"CREATED and verified on screen: {rec.name}, company {rec.account_name or '-'}, phone {rec.phone or '-'}, "
                    f"email {rec.email or '-'}. Now call finish(success=true, ...).")
        return f"PROBLEM: {res.message}"

    async def _exec(self, task: BrowserTask, name: str, args: dict[str, Any]) -> str:
        tl = self.tools
        try:
            if name == "finish":
                ok = bool(args.get("success"))
                task.status = "done" if ok else "failed"
                task.summary = str(args.get("summary") or "")
                await self._event(task, "done" if ok else "failed", task.summary)
                return "finished"
            if name == "report_progress":
                await self._event(task, "progress", str(args.get("message", ""))[:120])
                return "ok"
            if name == "ask_caller":
                q = str(args.get("question", ""))
                task.status, task.pending_question = "waiting_for_caller", q
                await self._event(task, "question", q)
                answer = await asyncio.wait_for(task.inbox.get(), timeout=90)
                task.status, task.pending_question = "running", None
                await self._event(task, "answer", answer)
                return f"CALLER ANSWERED: {answer}\n\nCURRENT PAGE:\n{await tl.outline()}"
            if name in ("create_lead", "search_leads"):
                return await self._skill(task, name, args)
            if name == "look":
                return await tl.outline()
            action = {
                "click": lambda: tl.click(int(args["ref"])),
                "type_text": lambda: tl.type_text(int(args["ref"]), str(args["text"]), bool(args.get("submit"))),
                "select_option": lambda: tl.select_option(int(args["ref"]), str(args["option"])),
                "press_key": lambda: tl.press_key(str(args["key"])),
                "go_to": lambda: tl.go_to(str(args["target"])),
                "scroll": lambda: tl.scroll(str(args.get("direction", "down"))),
            }.get(name)
            if action is None:
                return f"unknown tool {name}"
            msg = await action()
            await self._event(task, "step", f"{name} {json.dumps(args)[:100]}")
            return f"{msg}\n\nCURRENT PAGE:\n{await tl.outline()}"
        except asyncio.TimeoutError:
            task.status, task.pending_question = "running", None
            return "No answer from the caller in time. Finish with what you have, or fail."
        except Exception as e:
            return f"ERROR: {e!s:.300}\n\nCURRENT PAGE:\n{await tl.outline()}"
