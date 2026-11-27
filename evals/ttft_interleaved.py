"""Latency-only check: alternate models round-robin on the same prompt so network
conditions affect all models equally. Prints TTFT p50/p90 per model."""
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm_eval import CASES, build_ctx, make_llm  # noqa: E402
from espo_voice_agent.agent import IntakeAgent  # noqa: E402

async def ttft(m, case, tools):
    t0 = time.perf_counter()
    async with m.chat(chat_ctx=build_ctx(case), tools=tools) as s:
        async for ch in s:
            if ch.delta and (ch.delta.content or ch.delta.tool_calls):
                return time.perf_counter() - t0

async def main():
    models = sys.argv[2:]
    rounds = int(sys.argv[1])
    tools = IntakeAgent(crm=None).tools
    llms = {m: make_llm(m) for m in models}
    res = {m: [] for m in models}
    cases = [c for c in CASES if c["id"] in ("confirm_basic", "no_readback_yet", "asr_email_words")]
    for r in range(rounds):
        for m in models:
            try:
                res[m].append(await asyncio.wait_for(ttft(llms[m], cases[r % len(cases)], tools), 30))
            except Exception as e:
                print(m, "error", e)
    out = {m: {"n": len(v), "p50": round(statistics.median(v), 3), "p90": round(sorted(v)[int(0.9 * (len(v) - 1))], 3)} for m, v in res.items() if v}
    print(json.dumps(out, indent=1))
    Path(__file__).parent.joinpath("results", "ttft_interleaved.json").write_text(json.dumps({"ran_at": time.strftime("%Y-%m-%d %H:%M"), "rounds": rounds, "results": out}, indent=1))

asyncio.run(main())
