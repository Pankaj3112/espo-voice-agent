"""EspoCRM browser driver.

The voice agent never clicks pixels or guesses selectors at run time. It calls a
small set of typed actions (search, create, read back) implemented here with
Playwright against EspoCRM's stable `data-name` field attributes. Every write is
verified by reading the saved record back from the detail view, so the agent
only tells the caller "done" when the CRM actually shows the data.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import asdict, dataclass, field

from playwright.async_api import Browser, BrowserContext, Page, Playwright

log = logging.getLogger("espo")

DEFAULT_BASE_URL = "https://demo.us.espocrm.com/"

LEAD_SOURCES = [
    "Call",
    "Email",
    "Existing Customer",
    "Partner",
    "Public Relations",
    "Web Site",
    "Campaign",
    "Other",
]

# Injected once per page: a visible highlight on the element being worked on,
# so someone watching the browser stream can follow what the agent is doing.
_HIGHLIGHT_CSS = """
.va-focus { outline: 3px solid #7c3aed !important; outline-offset: 2px !important;
  border-radius: 4px; transition: outline-color .2s; }
"""


@dataclass
class LeadInput:
    first_name: str
    last_name: str
    account_name: str | None = None
    phone: str | None = None
    email: str | None = None
    title: str | None = None
    source: str | None = "Call"
    description: str | None = None


@dataclass
class LeadRecord:
    id: str
    url: str
    name: str = ""
    account_name: str = ""
    phone: str = ""
    email: str = ""
    title: str = ""
    source: str = ""
    status: str = ""
    description: str = ""


@dataclass
class ActionResult:
    ok: bool
    message: str
    record: LeadRecord | None = None
    matches: list[dict] = field(default_factory=list)
    timings_ms: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class _Timer:
    def __init__(self) -> None:
        self.marks: dict[str, int] = {}
        self._t = time.perf_counter()

    def mark(self, name: str) -> None:
        now = time.perf_counter()
        self.marks[name] = round((now - self._t) * 1000)
        self._t = now


class EspoBrowser:
    """One logged-in EspoCRM tab. Create one per call/session."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        username: str = "admin",
        type_delay_ms: int = 12,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.username = username
        self.type_delay_ms = type_delay_ms
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self.page: Page | None = None
        self._lock = asyncio.Lock()  # one UI action at a time per tab

    # ---------- lifecycle ----------

    async def start(self, pw: Playwright, headless: bool = True) -> None:
        self._browser = await pw.chromium.launch(headless=headless)
        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 800}, device_scale_factor=1
        )
        self.page = await self._context.new_page()
        self.page.set_default_timeout(15000)
        await self.login()

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
        self._browser = self._context = self.page = None

    async def login(self) -> None:
        page = self._p
        await page.goto(self.base_url)
        await page.wait_for_selector("#btn-login")
        # The public demo's login form is pre-filled (Administrator, no password typed).
        if await page.locator("select#field-userName").count():
            await page.select_option("select#field-userName", self.username)
        await page.click("#btn-login")
        await page.wait_for_selector("#navbar", state="attached", timeout=20000)
        await page.add_style_tag(content=_HIGHLIGHT_CSS)
        log.info("logged in to %s", self.base_url)

    @property
    def _p(self) -> Page:
        if not self.page:
            raise RuntimeError("browser not started")
        return self.page

    # ---------- helpers ----------

    async def _goto_hash(self, hash_: str, ready_selector: str) -> None:
        # EspoCRM is a single-page app: changing the hash routes without a full
        # reload (~0.3s instead of ~2.5s for the list view).
        page = self._p
        await page.evaluate("(h) => { window.location.hash = h; }", hash_)
        await page.wait_for_function("(h) => window.location.hash === h", arg=hash_)
        await page.wait_for_selector(ready_selector)

    async def _focus(self, selector: str) -> None:
        await self._p.evaluate(
            """(sel) => { document.querySelectorAll('.va-focus').forEach(e => e.classList.remove('va-focus'));
                         const el = document.querySelector(sel); if (el) { el.classList.add('va-focus');
                         el.scrollIntoView({block: 'center', behavior: 'instant'}); } }""",
            selector,
        )

    async def _type(self, selector: str, value: str) -> None:
        await self._focus(selector)
        loc = self._p.locator(selector).first
        await loc.click()
        await loc.fill("")
        await loc.press_sequentially(value, delay=self.type_delay_ms)

    async def _select(self, field_name: str, value: str) -> None:
        """Pick a value in one of EspoCRM's selectize dropdowns."""
        page = self._p
        wrap = f'.record .field[data-name="{field_name}"]'
        await self._focus(wrap)
        await page.locator(f"{wrap} .selectize-input").click()
        option = page.locator(f'{wrap} .selectize-dropdown .option[data-value="{value}"]')
        await option.click()

    async def _read_detail_field(self, name: str) -> str:
        loc = self._p.locator(f'.record .field[data-name="{name}"]').first
        if not await loc.count():
            return ""
        return " ".join((await loc.inner_text()).split())

    # ---------- actions ----------

    async def search_leads(self, query: str, limit: int = 5) -> ActionResult:
        """Search the Leads list view with the global text filter, like a user would."""
        async with self._lock:
            t = _Timer()
            page = self._p
            await self._goto_hash("#Lead", '.search-container input[data-name="textFilter"]')
            t.mark("open_list")
            box = '.search-container input[data-name="textFilter"]'
            await self._type(box, query)
            # Wait for *this* search's request (not the list's initial load), then for
            # the table to re-render with rows or the empty-state message.
            def _is_search(r) -> bool:
                return "/api/v1/Lead" in r.url and "textFilter" in r.url and r.request.method == "GET"

            async with page.expect_response(_is_search):
                await page.press(box, "Enter")
            await page.wait_for_function(
                """(q) => { const box = document.querySelector('.search-container input[data-name="textFilter"]');
                           const list = document.querySelector('.list-container');
                           if (!list || !box || box.value !== q) return false;
                           return list.querySelector('tbody tr[data-id]') || /No Data/i.test(list.innerText); }""",
                arg=query,
            )
            await page.wait_for_timeout(150)
            t.mark("search")
            rows = page.locator(".list-container table tbody tr[data-id]")
            matches = []
            for i in range(min(await rows.count(), limit)):
                row = rows.nth(i)
                cells = {}
                for col in ("name", "accountName", "phoneNumber", "emailAddress", "status"):
                    c = row.locator(f'td[data-name="{col}"]')
                    cells[col] = " ".join((await c.inner_text()).split()) if await c.count() else ""
                cells["id"] = await row.get_attribute("data-id") or ""
                matches.append(cells)
            t.mark("read_rows")
            msg = f"{len(matches)} matching lead(s)" if matches else "no matching leads"
            return ActionResult(ok=True, message=msg, matches=matches, timings_ms=t.marks)

    async def find_duplicates(self, lead: LeadInput) -> ActionResult:
        """Fast duplicate check through the logged-in app's own AJAX layer (~0.4s).

        Same session and permissions as the UI; used before every create so the
        caller hears about an existing record before anything is written.
        """
        async with self._lock:
            t = _Timer()
            conds = [
                {
                    "type": "and",
                    "value": [
                        {"type": "equals", "attribute": "firstName", "value": lead.first_name},
                        {"type": "equals", "attribute": "lastName", "value": lead.last_name},
                    ],
                }
            ]
            if lead.email:
                conds.append({"type": "equals", "attribute": "emailAddress", "value": lead.email})
            rows = await self._p.evaluate(
                """async (where) => {
                    const res = await Espo.Ajax.getRequest('Lead', {
                        maxSize: 5, orderBy: 'createdAt', order: 'desc',
                        select: 'name,accountName,emailAddress,phoneNumber,status,createdAt',
                        where: [{type: 'or', value: where}],
                    });
                    return res.list;
                }""",
                conds,
            )
            t.mark("query")
            matches = [
                {
                    "id": r.get("id", ""),
                    "name": r.get("name") or "",
                    "accountName": r.get("accountName") or "",
                    "emailAddress": r.get("emailAddress") or "",
                    "phoneNumber": r.get("phoneNumber") or "",
                    "status": r.get("status") or "",
                    "createdAt": r.get("createdAt") or "",
                    "url": f"{self.base_url}#Lead/view/{r.get('id', '')}",
                }
                for r in rows
            ]
            msg = f"{len(matches)} possible duplicate(s)" if matches else "no duplicates"
            return ActionResult(ok=True, message=msg, matches=matches, timings_ms=t.marks)

    # Entities the agent may search; the in-page text filter matches each entity's name/email/phone fields.
    SEARCHABLE = ("Lead", "Contact", "Account", "Opportunity", "Meeting", "Call", "Task", "Case")
    _SUMMARY_FIELDS = ("accountName", "emailAddress", "phoneNumber", "status", "stage", "amount", "dateStart",
                       "dateEnd", "parentName", "closeDate", "website", "title")

    async def search_records(self, entity: str, query: str = "", limit: int = 6, field: str | None = None, value: str | None = None) -> ActionResult:
        """Search any entity through the logged-in app's own AJAX layer (same session and permissions as
        the UI, ~0.1 s). The browser agent then opens the record so the caller sees it on screen."""
        if entity not in self.SEARCHABLE:
            return ActionResult(ok=False, message=f"unknown entity {entity}")
        async with self._lock:
            t = _Timer()
            res = await self._p.evaluate(
                """async ([entity, q, n, field, value]) => {
                    const params = {maxSize: n, orderBy: 'createdAt', order: 'desc'};
                    if (q) params.textFilter = q;
                    if (field) params.where = [{type: 'equals', attribute: field, value: value}];
                    const r = await Espo.Ajax.getRequest(entity, params);
                    return {total: r.total, list: r.list};
                }""",
                [entity, query, limit, field, value],
            )
            t.mark("query")
            matches = []
            for r in res.get("list", []):
                row = {"id": r.get("id", ""), "name": r.get("name") or ""}
                row.update({k: r[k] for k in self._SUMMARY_FIELDS if r.get(k) not in (None, "", [])})
                matches.append(row)
            return ActionResult(ok=True, message=f"{res.get('total', len(matches))} total", matches=matches, timings_ms=t.marks)

    async def show_search(self, entity: str, query: str) -> None:
        """Make a search visible to the caller: open the entity's list and type the query into EspoCRM's own
        search box, like a person would. Best effort; the data itself comes from search_records."""
        if entity not in self.SEARCHABLE:
            return
        async with self._lock:
            page = self._p
            box = '.search-container input[data-name="textFilter"]'
            try:
                await self._goto_hash(f"#{entity}", box)
                if not query:
                    return
                await self._type(box, query)

                def _is_search(r) -> bool:
                    return f"/api/v1/{entity}" in r.url and "textFilter" in r.url and r.request.method == "GET"

                async with page.expect_response(_is_search, timeout=8000):
                    await page.press(box, "Enter")
                await page.wait_for_timeout(300)
            except Exception:
                log.warning("visible search failed for %s %r", entity, query, exc_info=True)

    async def create_lead(self, lead: LeadInput, allow_duplicate: bool = False) -> ActionResult:
        async with self._lock:
            t = _Timer()
            page = self._p
            await self._goto_hash("#Lead/create", '.record [data-name="firstName"]')
            t.mark("open_form")

            await self._type('.record input[data-name="firstName"]', lead.first_name)
            await self._type('.record input[data-name="lastName"]', lead.last_name)
            if lead.account_name:
                await self._type('.record input[data-name="accountName"]', lead.account_name)
            if lead.title:
                await self._type('.record input[data-name="title"]', lead.title)
            if lead.email:
                await self._type('.record .field[data-name="emailAddress"] input.email-address', lead.email)
            if lead.phone:
                await self._type('.record .field[data-name="phoneNumber"] input.phone-number', lead.phone)
            if lead.source and lead.source in LEAD_SOURCES:
                await self._select("source", lead.source)
            if lead.description:
                await self._type('.record textarea[data-name="description"]', lead.description)
            t.mark("fill")

            await self._focus('.record button[data-action="save"]')
            await page.locator('.record button[data-action="save"]').first.click()

            # Outcomes: navigates to the detail view, a duplicate-check modal, or a validation error.
            view = page.wait_for_url(re.compile(r"#Lead/view/"), timeout=15000)
            dup = page.wait_for_selector(".modal-dialog .modal-body", timeout=15000)
            done, pending = await asyncio.wait(
                [asyncio.ensure_future(view), asyncio.ensure_future(dup)],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for p in pending:
                p.cancel()
            if "#Lead/view/" not in page.url and allow_duplicate and await self._confirm_duplicate_modal():
                pass  # caller already agreed to add it despite the existing record
            if "#Lead/view/" not in page.url:
                modal_text = " ".join((await page.locator(".modal-dialog").first.inner_text()).split())
                cancel = page.locator('.modal-dialog button[data-name="cancel"]')
                if await cancel.count():
                    await cancel.first.click()
                return ActionResult(
                    ok=False,
                    message=f"EspoCRM did not save the lead: {modal_text[:300]}",
                    timings_ms=t.marks,
                )
            t.mark("save")

            await self._wait_detail_mode()
            record = await self._read_lead_detail()
            t.mark("read_back")
            problems = _compare(lead, record)
            if problems:
                return ActionResult(
                    ok=False,
                    message="Saved, but read-back differs: " + "; ".join(problems),
                    record=record,
                    timings_ms=t.marks,
                )
            return ActionResult(ok=True, message="Lead created and verified", record=record, timings_ms=t.marks)

    async def _confirm_duplicate_modal(self) -> bool:
        """EspoCRM's own duplicate warning: press its save button and wait for the detail view."""
        page = self._p
        modal = page.locator(".modal-dialog").first
        text = " ".join((await modal.inner_text()).lower().split())
        if "already exist" not in text and "duplicate" not in text:
            return False  # a validation error or something else: don't force it
        btn = modal.locator("button").filter(has_text=re.compile(r"^\s*(Create|Save)\s*$"))
        if not await btn.count():
            return False
        await btn.first.click()
        try:
            await page.wait_for_url(re.compile(r"#Lead/view/"), timeout=15000)
        except Exception:
            return False
        return True

    async def _wait_detail_mode(self) -> None:
        # After save the URL flips to #Lead/view/<id> before the edit form is swapped
        # for the read-only detail view; wait until the name field is rendered as text.
        await self._p.wait_for_function(
            """() => { const f = document.querySelector('.record .field[data-name="name"]');
                       return f && !f.querySelector('input') && f.innerText.trim().length > 0; }""",
            timeout=15000,
        )

    async def _read_lead_detail(self) -> LeadRecord:
        page = self._p
        m = re.search(r"#Lead/view/([A-Za-z0-9]+)", page.url)
        lead_id = m.group(1) if m else ""
        return LeadRecord(
            id=lead_id,
            url=f"{self.base_url}#Lead/view/{lead_id}",
            name=await self._read_detail_field("name"),
            account_name=await self._read_detail_field("accountName"),
            phone=_strip_phone_type(await self._read_detail_field("phoneNumber")),
            email=await self._read_detail_field("emailAddress"),
            title=await self._read_detail_field("title"),
            source=await self._read_detail_field("source"),
            status=await self._read_detail_field("status"),
            description=await self._read_detail_field("description"),
        )

    async def open_lead(self, lead_id: str) -> ActionResult:
        async with self._lock:
            await self._goto_hash(f"#Lead/view/{lead_id}", '.record .field[data-name="name"]')
            await self._wait_detail_mode()
            record = await self._read_lead_detail()
            return ActionResult(ok=True, message="Opened lead", record=record)


def _strip_phone_type(s: str) -> str:
    # detail view renders "+1 415 555 0142 Mobile"
    return re.sub(r"\s+(Mobile|Office|Home|Fax|Other)$", "", s)


def _digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def _compare(want: LeadInput, got: LeadRecord) -> list[str]:
    problems = []
    if want.first_name.lower() not in got.name.lower() or want.last_name.lower() not in got.name.lower():
        problems.append(f"name is '{got.name}'")
    if want.account_name and want.account_name.lower() != got.account_name.lower():
        problems.append(f"company is '{got.account_name}'")
    if want.email and want.email.lower() not in got.email.lower():
        problems.append(f"email is '{got.email}'")
    # EspoCRM may reformat phone numbers; compare digits only.
    if want.phone and _digits(want.phone)[-7:] not in _digits(got.phone):
        problems.append(f"phone is '{got.phone}'")
    return problems
