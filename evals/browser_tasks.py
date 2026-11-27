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


TASKS = [
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


def make_lead() -> dict:
    first = random.choice(["Mara", "Tomas", "Ines", "Kofi", "Lena", "Ravi"])
    last = random.choice(["Okonkwo", "Lindqvist", "Ferreira", "Nakamura", "Brandt", "Achebe"]) + str(random.randint(10, 99))
    company = random.choice(["Quarry Labs", "Bluefin Logistics", "Orbit Robotics", "Nimbus Analytics"])
    return {"firstName": first, "lastName": last, "accountName": company, "status": "New", "source": "Call",
            "_domain": company.split()[0].lower() + ".com"}


async def run_task(model: str, task: dict, page, lock) -> dict:
    lead = make_lead()
    fmt = {"name": f"{lead['firstName']} {lead['lastName']}", "first": lead["firstName"].lower(),
           "company": lead["accountName"], "domain": lead["_domain"]}
    async with httpx.AsyncClient(auth=AUTH, timeout=30) as c:
        r = await c.post(f"{API}/Lead", json={k: v for k, v in lead.items() if not k.startswith("_")})
        r.raise_for_status()
        lid = r.json()["id"]
        before = r.json()
        events: list[str] = []

        async def notify(t, kind, text):
            events.append(f"{kind}: {text}")
            print(f"    [{t.id} {kind}] {text}")

        agent = BrowserAgent(page, notify, lock, model=model)
        t0 = time.perf_counter()
        bt = agent.start(task["goal"].format(**fmt))
        while bt.status in ("running", "waiting_for_caller"):
            if bt.status == "waiting_for_caller":
                bt.inbox.put_nowait("I mean the one at " + fmt["company"] + ".")
            await asyncio.sleep(0.3)
        secs = time.perf_counter() - t0
        after = (await c.get(f"{API}/Lead/{lid}")).json()
        ok = bt.status == "done" and task["check"](after, fmt)
        if task.get("summary_has"):
            ok = ok and task["summary_has"].format(**fmt).lower() in bt.summary.lower()
        if task.get("unchanged"):
            ok = ok and after.get("modifiedAt") == before.get("modifiedAt")
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
            for task in TASKS:
                if args.tasks and task["id"] not in args.tasks:
                    continue
                print(f"== {model} / {task['id']}")
                try:
                    r = await run_task(model, task, b.page, b._lock)
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
