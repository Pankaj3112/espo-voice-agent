"""Text-level eval of the agent's brain: does the LLM call the right tool, with
the right fields, at the right moment? Runs the exact production prompt and
tool schemas (via the same LiveKit LLM plugin) on ASR-style transcripts.

Also records time-to-first-token per model, which is the LLM's share of the
voice response latency.

Usage:
  uv run python evals/llm_eval.py --models openai/gpt-6-luna anthropic/claude-haiku-5.5 --repeats 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from livekit.agents import llm as lkllm  # noqa: E402
from livekit.plugins import openai as lkopenai  # noqa: E402

from espo_voice_agent import config  # noqa: E402
from espo_voice_agent.agent import IntakeAgent  # noqa: E402
from espo_voice_agent.prompt import GREETING, INSTRUCTIONS  # noqa: E402

CASES = json.loads((Path(__file__).parent / "llm_cases.json").read_text())

# Upstream pinning per model family (production pins OpenAI for gpt-6-luna).
PROVIDERS = {"openai/": "openai", "anthropic/": "anthropic", "google/": "google-ai-studio", "mistralai/": "mistral"}


def make_llm(model: str) -> lkopenai.LLM:
    prov = next((p for k, p in PROVIDERS.items() if model.startswith(k)), None)
    kwargs = {}
    if model.startswith("openai/gpt-6"):
        kwargs["reasoning_effort"] = "none"
    return lkopenai.LLM.with_openrouter(
        model=model,
        provider={"order": [prov], "allow_fallbacks": True} if prov else None,
        temperature=0.2,
        **kwargs,
    )


def build_ctx(case: dict) -> lkllm.ChatContext:
    ctx = lkllm.ChatContext()
    ctx.add_message(role="system", content=INSTRUCTIONS)
    ctx.add_message(role="assistant", content=GREETING)
    for i, (role, body) in enumerate(case["turns"]):
        if role == "tool":  # a previous tool call + its result
            cid = f"call_{i}"
            ctx.items.append(lkllm.FunctionCall(call_id=cid, name=body["name"], arguments=json.dumps(body["args"])))
            ctx.items.append(lkllm.FunctionCallOutput(call_id=cid, name=body["name"], output=body["output"], is_error=False))
        else:
            ctx.add_message(role=role, content=body)
    return ctx


def norm_digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


def score(case: dict, calls: list[dict]) -> tuple[bool, str]:
    exp = case["expect"]
    if exp["tool"] is None:
        bad = [c for c in calls if c["name"] == "create_lead"]
        return (not bad, "unexpected create_lead" if bad else "ok")
    match = [c for c in calls if c["name"] == exp["tool"]]
    if not match:
        return False, f"no {exp['tool']} call (got {[c['name'] for c in calls]})"
    args = match[0]["args"]
    for k, want in exp.get("args", {}).items():
        got = args.get(k)
        if k == "phone":
            ok = norm_digits(got).endswith(norm_digits(want)[-10:]) and len(norm_digits(want)) > 0
        elif k == "query":
            ok = want.lower() in (got or "").lower()
        elif isinstance(want, bool):
            ok = bool(got) == want
        else:
            ok = (got or "").strip().lower() == want.lower()
        if not ok:
            return False, f"{k}: want {want!r} got {got!r}"
    return True, "ok"


def has_repeat(text: str) -> bool:
    """Spoken output must never say the same sentence twice (seen live with some models)."""
    sents = [re.sub(r"\W+", " ", x).strip().lower() for x in re.split(r"(?<=[.?!])\s+", text)]
    sents = [x for x in sents if len(x) > 12]
    return len(sents) != len(set(sents))


async def run_one(model_llm, case: dict, tools) -> dict:
    ctx = build_ctx(case)
    t0 = time.perf_counter()
    ttft = None
    text = ""
    calls: list[dict] = []
    async with model_llm.chat(chat_ctx=ctx, tools=tools) as stream:
        async for chunk in stream:
            d = chunk.delta
            if d is None:
                continue
            if ttft is None and (d.content or d.tool_calls):
                ttft = time.perf_counter() - t0
            text += d.content or ""
            for tc in d.tool_calls or []:
                try:
                    calls.append({"name": tc.name, "args": json.loads(tc.arguments or "{}")})
                except json.JSONDecodeError:
                    calls.append({"name": tc.name, "args": {}, "bad_json": tc.arguments})
    ok, why = score(case, calls)
    return {"id": case["id"], "ok": ok, "why": why, "repeated_sentence": has_repeat(text), "ttft": ttft, "total": time.perf_counter() - t0, "calls": calls, "text": text[:300]}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="+", default=[config.LLM_MODEL])
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--out", type=Path, default=Path(__file__).parent / "results" / "llm_eval.json")
    args = ap.parse_args()

    tools = IntakeAgent(crm=None).tools
    summary = {}
    all_rows = {}
    for model in args.models:
        m = make_llm(model)
        rows = []
        for rep in range(args.repeats):
            for case in CASES:
                try:
                    r = await asyncio.wait_for(run_one(m, case, tools), 60)
                except Exception as e:
                    r = {"id": case["id"], "ok": False, "why": f"error: {e!r}"[:200], "ttft": None, "total": None, "calls": []}
                r["rep"] = rep
                rows.append(r)
                print(f"{model:32s} {case['id']:24s} {'PASS' if r['ok'] else 'FAIL'}  ttft={r['ttft'] and round(r['ttft'], 2)}  {'' if r['ok'] else r['why']}")
        ttfts = sorted(r["ttft"] for r in rows if r["ttft"])
        summary[model] = {
            "pass": sum(r["ok"] for r in rows),
            "n": len(rows),
            "ttft_p50": round(statistics.median(ttfts), 3) if ttfts else None,
            "ttft_p90": round(ttfts[int(0.9 * (len(ttfts) - 1))], 3) if ttfts else None,
            "repeated_sentence": sum(bool(r.get("repeated_sentence")) for r in rows),
            "failures": sorted({f"{r['id']}: {r['why']}" for r in rows if not r["ok"]}),
        }
        all_rows[model] = rows
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "rows": all_rows, "ran_at": time.strftime("%Y-%m-%d %H:%M")}, indent=1))
    print("\n" + json.dumps(summary, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
