"""Eval for the browser agent alone (no voice): give it plain-language CRM tasks,
let it drive the real EspoCRM UI, then check the outcome through the REST API.

Each task gets a fresh test lead (created over REST, deleted afterwards), so runs
don't depend on each other or on other people's demo data.

Usage (on the server, which has Vertex credentials):
  python evals/browser_tasks.py --models gemini-3.8-flash gemini-3.1-pro-preview
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from espo_voice_agent import config  # noqa: E402
from espo_voice_agent.browser_agent import BrowserAgent  # noqa: E402
from espo_voice_agent.espo import EspoBrowser  # noqa: E402

API = config.ESPO_BASE_URL.rstrip("/") + "/api/v1"
AUTH = ("admin", "1")


def _digits(s: str | None) -> str:
    return "".join(c for c in (s or "") if c.isdigit())


async def _leads_by_last(c, last: str) -> list[dict]:
    r = await c.get(f"{API}/Lead", params={"where[0][type]": "equals", "where[0][attribute]": "lastName", "where[0][value]": last, "select": "id,name,accountName,emailAddress,phoneNumber"})
    return r.json().get("list", [])


TASKS = [
    {
        "id": "create_new",
        "goal": "Create a new lead: first name {new_first}, last name {new_last}, company {company}, phone 415 555 0177, email {new_email}.",
        "api_check": lambda c, f: _leads_by_last(c, f["new_last"]),
        "api_ok": lambda rows, f: len(rows) == 1 and (rows[0].get("emailAddress") or "").lower() == f["new_email"] and _digits(rows[0].get("phoneNumber")).endswith("4155550177"),
    },
    {
        "id": "create_duplicate",
        "goal": "Create a new lead: first name {first_cap}, last name {last}, company {company}.",
        "answer": "Yes, create a new one anyway.",
        "needs_question": True,
        "api_check": lambda c, f: _leads_by_last(c, f["last"]),
        "api_ok": lambda rows, f: len(rows) == 2,
    },
    {
        "id": "edit_phone",
        "goal": "Add the phone number 415 555 0199 to the lead {name} at {company}.",
        "check": lambda r, f: _digits(r.get("phoneNumber")).endswith("4155550199"),
    },
    {
        "id": "edit_email",
        "goal": "Change the email of the lead {name} ({company}) to {first}.new@{domain}.",
        "check": lambda r, f: (r.get("emailAddress") or "").lower() == f"{f['first']}.new@{f['domain']}",
    },
    {
        "id": "set_title",
        "goal": "Set the job title of lead {name} at {company} to Head of Operations.",
        "check": lambda r, f: (r.get("title") or "").strip().lower() == "head of operations",
    },
    {
        "id": "status",
        "goal": "Change the status of the lead {name} from {company} to In Process.",
        "check": lambda r, f: r.get("status") == "In Process",
    },
    {
        "id": "add_note",
        "goal": "On the lead {name} at {company}, put this in the description: Interested in a pilot in Q1.",
        "check": lambda r, f: "pilot in q1" in (r.get("description") or "").lower(),
    },
    {
        "id": "read_only",
        "goal": "Look up the lead {name} and tell me which company they are with. Do not change anything.",
        "check": lambda r, f: True,  # verified via summary text below
        "summary_has": "{company}",
        "unchanged": True,
    },
]


async def _find(c, entity: str, attr: str, value: str) -> list[dict]:
    r = await c.get(f"{API}/{entity}", params={"where[0][type]": "equals", "where[0][attribute]": attr, "where[0][value]": value})
    try:
        return r.json().get("list", [])
    except ValueError:
        print(f"    [harness] {entity} search returned {r.status_code}: {r.text[:120]!r}")
        return []


async def _total(c, entity: str, attr: str, value: str) -> int:
    r = await c.get(f"{API}/{entity}", params={"where[0][type]": "equals", "where[0][attribute]": attr, "where[0][value]": value, "maxSize": 1})
    return int(r.json().get("total", -1))


async def _mk(c, f, entity: str, data: dict) -> dict:
    r = await c.post(f"{API}/{entity}", json=data, headers={"X-Skip-Duplicate-Check": "true"})
    r.raise_for_status()
    rec = r.json()
    f["_cleanup"].append((entity, rec["id"]))
    return rec


# ---- CRM-wide tasks (beyond leads). setup(c, f, lid) prepares data; verify(c, f, lid, summary) -> bool.
async def _setup_twin(c, f, lid):
    await c.put(f"{API}/Lead/{lid}", json={"emailAddress": f"{f['first']}.one@{f['domain']}"})
    await _mk(c, f, "Lead", {"firstName": f["first_cap"], "lastName": f["last"], "accountName": f["company"],
                             "emailAddress": f"{f['first']}.two@{f['domain']}"})
    f["answer"] = f"The one with the email {f['first']}.one@{f['domain']}."


async def _verify_twin(c, f, lid, summary):
    rows = await _find(c, "Lead", "lastName", f["last"])
    one = [r for r in rows if r["id"] == lid]
    two = [r for r in rows if r["id"] != lid]
    return bool(one) and (one[0].get("title") or "").lower() == "vp sales" and all(not r.get("title") for r in two)


async def _setup_account(c, f, lid):
    acc = await _mk(c, f, "Account", {"name": f"Evaltest Account {f['tag']}"})
    f["acct"], f["acct_id"] = acc["name"], acc["id"]


async def _verify_new_account(c, f, lid, summary):
    rows = await _find(c, "Account", "name", f"Evaltest Corp {f['tag']}")
    for r in rows:
        f["_cleanup"].append(("Account", r["id"]))
    return len(rows) == 1 and "evaltest" in (rows[0].get("website") or "").lower()


async def _verify_contact(c, f, lid, summary):
    rows = await _find(c, "Contact", "lastName", f"Evalcontact{f['tag']}")
    for r in rows:
        f["_cleanup"].append(("Contact", r["id"]))
    return len(rows) == 1 and rows[0].get("accountId") == f["acct_id"] and (rows[0].get("emailAddress") or "").lower() == f"kim.{f['tag']}@evaltest.com"


async def _verify_task(c, f, lid, summary):
    rows = await _find(c, "Task", "name", f"Send pricing {f['tag']}")
    for r in rows:
        f["_cleanup"].append(("Task", r["id"]))
    return len(rows) == 1 and rows[0].get("parentId") == lid


async def _verify_meeting(c, f, lid, summary):
    rows = await _find(c, "Meeting", "name", f"Intro {f['tag']}")
    for r in rows:
        f["_cleanup"].append(("Meeting", r["id"]))
    if len(rows) != 1:
        return False
    m = rows[0]
    print(f"    [verify] meeting dateStart={m.get('dateStart')} dateEnd={m.get('dateEnd')} parent={m.get('parentType')}:{m.get('parentId')} lead={lid}")
    import datetime as dt
    s0 = dt.datetime.fromisoformat(m["dateStart"])
    s1 = dt.datetime.fromisoformat(m["dateEnd"])
    want = dt.date.fromisoformat(f["meet_date"])
    full = (await c.get(f"{API}/Meeting/{m['id']}")).json()
    linked = m.get("parentId") == lid or lid in (full.get("leadsIds") or [])  # parent or attendee both count
    # The CRM runs in UTC and the task says 3 PM, so the start must be exactly 15:00 UTC on that date.
    return linked and s0 == dt.datetime.combine(want, dt.time(15, 0)) and (s1 - s0) == dt.timedelta(minutes=30)


async def _verify_opp(c, f, lid, summary):
    rows = await _find(c, "Opportunity", "name", f"Pilot {f['tag']}")
    print(f"    [verify] opportunities: {[(r.get('accountId'), r.get('amount'), r.get('stage')) for r in rows]} want account {f['acct_id']}")
    for r in rows:
        f["_cleanup"].append(("Opportunity", r["id"]))
    return (len(rows) == 1 and rows[0].get("accountId") == f["acct_id"] and float(rows[0].get("amount") or 0) == 5000
            and rows[0].get("stage") == "Prospecting")


async def _verify_convert(c, f, lid, summary):
    lead = (await c.get(f"{API}/Lead/{lid}")).json()
    rows = await _find(c, "Contact", "lastName", f["last"])
    for r in rows:
        f["_cleanup"].append(("Contact", r["id"]))
    if lead.get("createdAccountId"):
        f["_cleanup"].append(("Account", lead["createdAccountId"]))
    if lead.get("createdOpportunityId"):
        f["_cleanup"].append(("Opportunity", lead["createdOpportunityId"]))
    # Asked for a contact only: no account or opportunity may be created along the way.
    return (lead.get("status") == "Converted" and len(rows) == 1
            and not lead.get("createdAccountId") and not lead.get("createdOpportunityId"))


async def _verify_count(c, f, lid, summary):
    n = await _total(c, "Opportunity", "stage", "Negotiation")
    import re as _re
    nums = [int(x) for x in _re.findall(r"\b\d+\b", summary)]
    return nums == [n]  # exactly one number in the answer, and it is the real total


CRM_TASKS = [
    {"id": "ambiguous_edit", "setup": _setup_twin, "needs_question": True, "verify": _verify_twin,
     "goal": "Set the job title of the lead {name} at {company} to VP Sales."},
    {"id": "new_account", "verify": _verify_new_account,
     "goal": "Create a new account named Evaltest Corp {tag} with the website evaltest-{tag}.com."},
    {"id": "contact_at_account", "setup": _setup_account, "verify": _verify_contact,
     "goal": "Add a new contact Kim Evalcontact{tag} at the account {acct}, email kim.{tag}@evaltest.com."},
    {"id": "task_for_lead", "verify": _verify_task,
     "goal": "Create a task called 'Send pricing {tag}' for the lead {name} at {company}."},
    {"id": "meeting", "verify": _verify_meeting,
     "goal": "Schedule a meeting called 'Intro {tag}' with the lead {name} at {company} on {meet_human} at 3 PM, for 30 minutes."},
    {"id": "opportunity", "setup": _setup_account, "verify": _verify_opp, "answer": "Close date is {close_human}.",
     "goal": "Create an opportunity named 'Pilot {tag}' for the account {acct} with amount 5000 and stage Prospecting."},
    {"id": "convert_lead", "verify": _verify_convert,
     "goal": "Convert the lead {name} at {company} into a contact."},
    {"id": "count_question", "verify": _verify_count, "unchanged": True,
     "goal": "How many opportunities are in the Negotiation stage? Don't change anything."},
]


def make_lead() -> dict:
    first = random.choice(["Mara", "Tomas", "Ines", "Kofi", "Lena", "Ravi"])
    last = random.choice(["Okonkwo", "Lindqvist", "Ferreira", "Nakamura", "Brandt", "Achebe"]) + str(random.randint(10, 99))
    company = random.choice(["Quarry Labs", "Bluefin Logistics", "Orbit Robotics", "Nimbus Analytics"])
    return {"firstName": first, "lastName": last, "accountName": company, "status": "New", "source": "Call",
            "_domain": company.split()[0].lower() + ".com"}


async def run_task(model: str, task: dict, page, browser) -> dict:
    lead = make_lead()
    new_last = "Testcreate" + str(random.randint(1000, 9999))
    fmt = {"name": f"{lead['firstName']} {lead['lastName']}", "first": lead["firstName"].lower(), "first_cap": lead["firstName"],
           "last": lead["lastName"], "company": lead["accountName"], "domain": lead["_domain"],
           "new_first": "Nora", "new_last": new_last, "new_email": f"nora.{new_last.lower()}@{lead['_domain']}",
           "tag": str(random.randint(10000, 99999)), "_cleanup": []}
    import datetime as dt
    d = dt.date.today() + dt.timedelta(days=3)
    fmt["meet_date"], fmt["meet_human"] = d.isoformat(), d.strftime("%A %B %d, %Y")
    fmt["close_human"] = (dt.date.today() + dt.timedelta(days=45)).strftime("%B %d, %Y")
    async with httpx.AsyncClient(auth=AUTH, timeout=30) as c:
        r = await c.post(f"{API}/Lead", json={k: v for k, v in lead.items() if not k.startswith("_")})
        r.raise_for_status()
        lid = r.json()["id"]
        before = r.json()
        if task.get("setup"):
            await task["setup"](c, fmt, lid)
            before = (await c.get(f"{API}/Lead/{lid}")).json()
        events: list[str] = []

        async def notify(t, kind, text):
            events.append(f"{kind}: {text}")
            print(f"    [{t.id} {kind}] {text}")

        agent = BrowserAgent(page, notify, asyncio.Lock(), model=model, skills=browser)
        asked = False
        t0 = time.perf_counter()
        bt = agent.start(task["goal"].format(**fmt))
        bt.writes = task["id"] not in ("read_only", "count_question")  # as the voice agent would flag it
        while bt.status in ("running", "waiting_for_caller"):
            if bt.status == "waiting_for_caller" and bt.inbox.empty():
                asked = True
                bt.inbox.put_nowait((fmt.get("answer") or task.get("answer") or "I mean the one at {company}.").format(**fmt))
            await asyncio.sleep(0.3)
        secs = time.perf_counter() - t0
        after = (await c.get(f"{API}/Lead/{lid}")).json()
        if "api_check" in task:
            rows = await task["api_check"](c, fmt)
            ok = bt.status == "done" and task["api_ok"](rows, fmt) and (asked or not task.get("needs_question"))
            for row in rows:  # clean up anything the task created
                if row["id"] != lid:
                    await c.delete(f"{API}/Lead/{row['id']}")
        elif "verify" in task:
            ok = bt.status == "done" and await task["verify"](c, fmt, lid, bt.summary) and (asked or not task.get("needs_question"))
        else:
            ok = bt.status == "done" and task["check"](after, fmt)
        if task.get("summary_has"):
            ok = ok and task["summary_has"].format(**fmt).lower() in bt.summary.lower()
        if task.get("unchanged"):
            ok = ok and after.get("modifiedAt") == before.get("modifiedAt")
        for entity, rid in fmt["_cleanup"]:
            await c.delete(f"{API}/{entity}/{rid}")
        await c.delete(f"{API}/Lead/{lid}")
    return {"model": model, "task": task["id"], "ok": bool(ok), "status": bt.status, "summary": bt.summary,
            "seconds": round(secs, 1), "steps": bt.steps, "llm_ms_p50": sorted(bt.llm_ms)[len(bt.llm_ms) // 2] if bt.llm_ms else None,
            "events": events[-12:]}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=[config.BROWSER_LLM_MODEL])
    ap.add_argument("--tasks", nargs="*")
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "results" / "browser_tasks.json")
    args = ap.parse_args()
    rows = []
    async with async_playwright() as pw:
        b = EspoBrowser(config.ESPO_BASE_URL)
        await b.start(pw)
        for model in args.models:
            for task in TASKS + CRM_TASKS:
                if args.tasks and task["id"] not in args.tasks:
                    continue
                print(f"== {model} / {task['id']}")
                try:
                    r = await run_task(model, task, b.page, b)
                except Exception as e:
                    r = {"model": model, "task": task["id"], "ok": False, "status": "harness_error", "summary": repr(e)[:200]}
                print(f"   -> {'PASS' if r['ok'] else 'FAIL'} {r.get('seconds')}s steps={r.get('steps')} llm_p50={r.get('llm_ms_p50')}ms :: {r['summary'][:140]}")
                rows.append(r)
        await b.close()
    summary = {}
    for m in args.models:
        rs = [r for r in rows if r["model"] == m]
        secs = sorted(r["seconds"] for r in rs if r.get("seconds"))
        summary[m] = {"pass": sum(r["ok"] for r in rs), "n": len(rs), "median_s": secs[len(secs) // 2] if secs else None,
                      "max_s": max(secs) if secs else None}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "rows": rows, "ran_at": time.strftime("%Y-%m-%d %H:%M")}, indent=1))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
