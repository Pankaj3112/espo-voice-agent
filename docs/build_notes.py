"""Build docs/technical-notes.pdf: a short, visual write-up generated from the eval result files.

  uv run python docs/build_notes.py

Every number on a chart is read from evals/results/*.json or the constants below (measured call figures, with
their source noted), so the PDF can't drift from the data.
"""

from __future__ import annotations

import asyncio
import base64
import html
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RES = ROOT / "evals" / "results"
ASSETS = ROOT / "docs" / "assets"

# ---- data -------------------------------------------------------------------------------------------------
import os  # noqa: E402

BT = json.loads((RES / os.environ.get("BT_FILE", "browser_tasks_final.json")).read_text())
LLM = json.loads((RES / os.environ.get("LLM_FILE", "llm_eval_final.json")).read_text())
LLM_SUM = next(iter(LLM["summary"].values()))
BT_SUM = next(iter(BT["summary"].values()))

# Real calls through the public page (server-side timeline: caller stops speaking -> first agent audio sent).
# Source: logs on the server for the calls named; medians per call.
LATENCY_STEPS = [  # (label, median seconds, note)
    ("Home server · Nova-3 + text turn detector · LLM via OpenRouter", 2.9, "range 2.0–3.8 s"),
    ("Cloud VM in US East · Deepgram Flux turn detection", 1.66, ""),
    ("+ Gemini 3.1 Flash-Lite on Vertex (no extra hop)", 1.46, ""),
    ("+ Fish Audio voice", 1.53, "warmer voice, ~0.1 s slower first audio"),
    ("Final build · last test call", 1.29, "10 replies, 1.12–2.07 s"),
]
TURN = [("End of turn", 0.39, "Deepgram Flux"), ("LLM first token", 0.58, "Gemini 3.1 Flash-Lite"), ("First audio", 0.25, "Fish S2.1 Pro")]

MODELS = [  # (label, pass, n, first-token p50 s) from evals/results/llm_eval*.json
    ("Gemini 3.1 Flash-Lite · Vertex", LLM_SUM["pass"], LLM_SUM["n"], LLM_SUM["ttft_p50"]),
    ("Gemini 3.6 Flash · OpenRouter", 50, 50, 1.26),
    ("Gemini 3.8 Flash · Vertex", 50, 50, 2.09),
    ("Gemini 3.7 Flash", 50, 50, 2.16),
    ("Gemini 3.5 Flash · Vertex", 48, 50, 2.94),
    ("Gemini 3.5 Flash-Lite", 39, 50, 0.62),
    ("Claude Haiku 5.5", 48, 50, 2.02),
    ("Claude Haiku 4.5", 48, 50, 2.36),
    ("GPT-6 Luna", 47, 50, 1.34),
]

TASK_LABELS = {
    "create_new": "Create a lead", "create_duplicate": "Duplicate → asks first", "edit_phone": "Edit phone",
    "edit_email": "Edit email", "set_title": "Set job title", "status": "Change status", "add_note": "Add a note",
    "read_only": "Look up a company", "ambiguous_edit": "Two matches → asks which", "new_account": "New account",
    "contact_at_account": "Contact linked to account", "task_for_lead": "Task on a lead",
    "meeting": "30-min meeting at 3 PM", "opportunity": "Opportunity", "convert_lead": "Convert lead → contact",
    "count_question": "“How many in Negotiation?”",
}

# ---- palette (reference instance, light mode; validated) -----------------------------------------------------
BLUE, ORANGE, AQUA, VIOLET, RED = "#2a78d6", "#eb6834", "#1baf7a", "#6250d6", "#e34948"
INK, INK2, MUTED, LINE, SURF = "#0b0b0b", "#52514e", "#8a8984", "#e7e6e2", "#fcfcfb"


def img(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


def esc(s) -> str:
    return html.escape(str(s))


# ---- charts (inline SVG) -----------------------------------------------------------------------------------
def turn_bar(W: int = 440) -> str:
    """One stacked bar: where a typical reply's time goes."""
    h = 104
    total = sum(v for _, v, _ in TURN)
    scale = (W - 70) / total
    colors = [BLUE, VIOLET, AQUA]
    x, parts = 0.0, []
    for (lab, v, sub), c in zip(TURN, colors):
        bw = v * scale
        parts.append(f'<rect x="{x:.1f}" y="6" width="{bw - 3:.1f}" height="30" rx="4" fill="{c}"/>')
        parts.append(f'<text x="{x:.1f}" y="56" class="t">{esc(lab)}</text>')
        parts.append(f'<text x="{x:.1f}" y="73" class="v">{v:.2f} s</text>')
        parts.append(f'<text x="{x:.1f}" y="89" class="s">{esc(sub)}</text>')
        x += bw
    parts.append(f'<text x="{x + 6:.1f}" y="26" class="vb">{total:.2f} s</text>')
    return f'<svg viewBox="0 0 {W} {h}" class="chart">{"".join(parts)}</svg>'


def progress_chart(W: int = 440) -> str:
    """Bars: reply time at each milestone (lower is better)."""
    row = 40
    h = row * len(LATENCY_STEPS) + 22
    mx = 3.0
    left = 0
    sc = (W - 60) / mx
    out = []
    for i, (lab, v, note) in enumerate(LATENCY_STEPS):
        y = i * row
        last = i == len(LATENCY_STEPS) - 1
        c = BLUE if last else "#b9cfee"
        out.append(f'<text x="{left}" y="{y + 11}" class="s">{esc(lab)}{(" — " + esc(note)) if note else ""}</text>')
        out.append(f'<rect x="{left}" y="{y + 16}" width="{v * sc:.1f}" height="16" rx="4" fill="{c}"/>')
        out.append(f'<text x="{left + v * sc + 6:.1f}" y="{y + 29}" class="{"vb" if last else "v"}">{v:.2f} s</text>')
    for xv in (1.0, 1.5):
        x = left + xv * sc
        out.append(f'<line x1="{x:.1f}" y1="12" x2="{x:.1f}" y2="{h - 16}" stroke="#cfcdc7" stroke-dasharray="3 3"/>')
        out.append(f'<text x="{x:.1f}" y="{h - 3}" class="s" text-anchor="middle">{xv:g} s</text>')
    return f'<svg viewBox="0 0 {W} {h}" class="chart">{"".join(out)}</svg>'


def tasks_chart(W: int = 440) -> str:
    rows = sorted(BT["rows"], key=lambda r: r.get("seconds") or 0)
    row, left = 23, 168
    h = row * len(rows) + 4
    mx = max((r.get("seconds") or 0) for r in rows) * 1.15
    sc = (W - left - 70) / mx
    out = []
    for i, r in enumerate(rows):
        y = i * row
        sec = r.get("seconds") or 0
        ok = r["ok"]
        c = AQUA if ok else RED
        out.append(f'<text x="{left - 8}" y="{y + 14}" class="t" text-anchor="end">{esc(TASK_LABELS.get(r["task"], r["task"]))}</text>')
        out.append(f'<rect x="{left}" y="{y + 4}" width="{max(sec * sc, 3):.1f}" height="14" rx="4" fill="{c}"/>')
        out.append(f'<text x="{left + sec * sc + 6:.1f}" y="{y + 15}" class="v">{sec:.1f} s{"" if ok else " · failed"}</text>')
    return f'<svg viewBox="0 0 {W} {h}" class="chart">{"".join(out)}</svg>'


def models_html() -> str:
    """Ranked list: accuracy pill + speed bar per model; the chosen one is highlighted."""
    rows = [MODELS[0]] + sorted(MODELS[1:], key=lambda m: (-(m[1] / m[2]), m[3]))  # chosen first, then by accuracy
    mx = max(m[3] for m in MODELS)
    out = []
    for lab, p, n, t in rows:
        chosen = lab == MODELS[0][0]
        pct = round(100 * p / n)
        cls = "good" if pct == 100 else ("ok" if pct >= 94 else "bad")
        out.append(
            f'<div class="mrow{" chosen" if chosen else ""}"><span class="mname">{esc(lab)}</span>'
            f'<span class="acc {cls}">{p}/{n}</span>'
            f'<span class="mbar"><i style="width:{100 * t / mx:.0f}%"></i></span><span class="mt">{t:.2f} s</span></div>')
    return '<div class="models"><div class="mrow mhead"><span>model</span><span>correct</span><span>time to first word</span><span></span></div>' + "".join(out) + "</div>"


# ---- diagrams ----------------------------------------------------------------------------------------------
ARCH = f"""
<svg viewBox="0 0 1000 340" class="diagram">
  <defs>
    <marker id="ar" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{INK2}"/></marker>
    <marker id="arb" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{BLUE}"/></marker>
  </defs>
  <rect x="0" y="110" width="140" height="110" rx="14" fill="#fff" stroke="{LINE}"/>
  <text x="70" y="148" class="h" text-anchor="middle">You</text>
  <text x="70" y="170" class="s" text-anchor="middle">browser or phone</text>
  <text x="70" y="186" class="s" text-anchor="middle">voice · captions</text>
  <text x="70" y="202" class="s" text-anchor="middle">live browser view</text>

  <rect x="200" y="30" width="330" height="270" rx="16" fill="#f3f7fd" stroke="#cfe0f5"/>
  <text x="220" y="60" class="h">Ava · the voice agent</text>
  <text x="220" y="78" class="s">talks and confirms; never touches the CRM</text>
  <g class="pill"><rect x="220" y="96" width="290" height="34" rx="9"/><text x="234" y="118">Hearing + turn-taking · Deepgram Flux</text></g>
  <g class="pill"><rect x="220" y="140" width="290" height="34" rx="9"/><text x="234" y="162">Conversation · Gemini 3.1 Flash-Lite</text></g>
  <g class="pill"><rect x="220" y="184" width="290" height="34" rx="9"/><text x="234" y="206">Voice with emotion · Fish Audio</text></g>
  <g class="pill"><rect x="220" y="228" width="290" height="34" rx="9"/><text x="234" y="250">Noise cancellation · smart interruptions</text></g>

  <rect x="630" y="30" width="200" height="270" rx="16" fill="#f4f2fd" stroke="#dcd6f7"/>
  <text x="648" y="60" class="h">Browser agent</text>
  <text x="648" y="78" class="s">does the CRM work</text>
  <g class="pill"><rect x="646" y="96" width="168" height="34" rx="9"/><text x="660" y="118">Reads the page</text></g>
  <g class="pill"><rect x="646" y="140" width="168" height="34" rx="9"/><text x="660" y="162">Clicks · types · selects</text></g>
  <g class="pill"><rect x="646" y="184" width="168" height="34" rx="9"/><text x="660" y="206">Fast skills: create, search</text></g>
  <g class="pill"><rect x="646" y="228" width="168" height="34" rx="9"/><text x="660" y="250">Checks the saved record</text></g>

  <rect x="880" y="110" width="120" height="110" rx="14" fill="#fff" stroke="{LINE}"/>
  <text x="940" y="148" class="h" text-anchor="middle">EspoCRM</text>
  <text x="940" y="170" class="s" text-anchor="middle">in a real Chrome</text>
  <text x="940" y="186" class="s" text-anchor="middle">(Playwright)</text>

  <path d="M140 150 L198 150" stroke="{INK2}" stroke-width="2" marker-end="url(#ar)" fill="none"/>
  <path d="M198 180 L142 180" stroke="{INK2}" stroke-width="2" marker-end="url(#ar)" fill="none"/>
  <text x="170" y="140" class="s" text-anchor="middle">speech</text>
  <path d="M530 130 L628 130" stroke="{BLUE}" stroke-width="2.5" marker-end="url(#arb)" fill="none"/>
  <text x="580" y="104" class="sb" text-anchor="middle">the job,</text>
  <text x="580" y="118" class="sb" text-anchor="middle">in plain words</text>
  <path d="M628 200 L532 200" stroke="{BLUE}" stroke-width="2.5" stroke-dasharray="6 4" marker-end="url(#arb)" fill="none"/>
  <text x="580" y="222" class="sb" text-anchor="middle">progress,</text>
  <text x="580" y="236" class="sb" text-anchor="middle">questions,</text>
  <text x="580" y="250" class="sb" text-anchor="middle">verified result</text>
  <path d="M830 165 L878 165" stroke="{INK2}" stroke-width="2" marker-end="url(#ar)" fill="none"/>
  <path d="M940 222 C940 330 70 330 70 224" stroke="{MUTED}" stroke-width="1.5" stroke-dasharray="4 4" marker-end="url(#ar)" fill="none"/>
  <text x="500" y="328" class="s" text-anchor="middle">you watch the agent's browser live (screen stream)</text>
</svg>"""

FLOW_STEPS = [
    ("1", "You ask", "“Change Jane's title to Head of Sales.”"),
    ("2", "Ava reads it back", "“I'll set Jane Doe's title to Head of Sales, okay?”"),
    ("3", "You say yes", "a yes is checked in code, used once"),
    ("4", "Browser agent works", "opens the record, edits, saves — live on screen"),
    ("5", "It double-checks", "re-reads the saved record before reporting"),
    ("6", "Ava confirms", "only what was verified — in her own words"),
]

GUARDS = [
    ("Clear yes", "Questions, “yes but…”, and corrections don't count. One yes = one change."),
    ("Values match", "Emails and phones written must equal what you heard read back."),
    ("Rules in code", "Look-ups can't write. Delete & mass actions blocked at the network."),
    ("Real save", "No “done” unless a save actually reached the CRM."),
    ("Verified", "Every requested value is re-read on the saved record."),
    ("Grounded", "Ava sees the job's real status every turn; it can't claim early."),
]


def flow_html() -> str:
    cells = "".join(
        f'<div class="step"><span class="num">{n}</span><b>{esc(t)}</b><span>{esc(d)}</span></div>' for n, t, d in FLOW_STEPS)
    return f'<div class="flow">{cells}</div>'


def guards_html() -> str:
    return '<div class="guards">' + "".join(
        f'<div class="guard"><span class="chk">✓</span><b>{esc(t)}</b><span>{esc(d)}</span></div>' for t, d in GUARDS) + "</div>"


def outline_snippet() -> str:
    keep = []
    for line in (ASSETS / "outline_sample.txt").read_text().splitlines():
        if line.startswith(("<page", "URL", "TITLE")):
            continue
        keep.append(line)
        if len(keep) >= 15:
            break
    out = []
    for line in keep:
        e = esc(line)
        if line.startswith("["):
            ref, rest = e.split("]", 1)
            e = f'<span class="ref">{ref}]</span>{rest}'
        elif line.startswith("#"):
            e = f'<span class="hd">{e}</span>'
        out.append(e)
    return "<br>".join(out)


# ---- page --------------------------------------------------------------------------------------------------
def build() -> str:
    kpi_llm = f'{LLM_SUM["pass"]}/{LLM_SUM["n"]}'
    kpi_bt = f'{BT_SUM["pass"]}/{BT_SUM["n"]}'
    failed = [r for r in BT["rows"] if not r["ok"]]
    fail_note = (" · " + ", ".join(TASK_LABELS.get(r["task"], r["task"]) for r in failed) + " failed") if failed else ""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
@page {{ size: A4 landscape; margin: 0; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font: 11pt/1.45 Inter, sans-serif; color: {INK}; background: {SURF}; }}
.page {{ width: 297mm; height: 210mm; padding: 14mm 16mm 12mm; page-break-after: always; position: relative; overflow: hidden; }}
.page:last-child {{ page-break-after: auto; }}
.kicker {{ font: 600 9pt Inter; letter-spacing: .12em; text-transform: uppercase; color: {BLUE}; margin-bottom: 4mm; }}
h1 {{ font-size: 34pt; line-height: 1.05; letter-spacing: -.03em; margin: 0 0 4mm; font-weight: 700; }}
h2 {{ font-size: 22pt; letter-spacing: -.02em; margin: 0 0 2mm; font-weight: 700; }}
.lede {{ font-size: 13pt; color: {INK2}; max-width: 175mm; margin: 0 0 7mm; }}
.sub {{ color: {INK2}; font-size: 11pt; margin: 0 0 6mm; max-width: 220mm; }}
.foot {{ position: absolute; left: 16mm; right: 16mm; bottom: 7mm; display: flex; justify-content: space-between; font-size: 8pt; color: {MUTED}; }}
.kpis {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 5mm; margin: 0 0 7mm; }}
.kpi {{ background: #fff; border: 1px solid {LINE}; border-radius: 4mm; padding: 5mm; }}
.kpi b {{ display: block; font-size: 26pt; letter-spacing: -.03em; line-height: 1; margin-bottom: 2mm; }}
.kpi span {{ color: {INK2}; font-size: 9.5pt; }}
.cover {{ display: grid; grid-template-columns: 1.05fr 1fr; gap: 10mm; align-items: center; }}
.shot {{ border-radius: 4mm; overflow: hidden; border: 1px solid {LINE}; box-shadow: 0 8px 28px rgba(0,0,0,.10); background: #fff; }}
.shot .bar {{ height: 8mm; display: flex; align-items: center; gap: 1.6mm; padding: 0 3mm; border-bottom: 1px solid {LINE}; background: #f6f6f4; }}
.shot .bar i {{ width: 2.4mm; height: 2.4mm; border-radius: 50%; background: #d9d8d4; display: block; }}
.shot .bar em {{ font: 8pt 'JetBrains Mono'; color: {MUTED}; margin-left: 3mm; font-style: normal; }}
.shot img {{ display: block; width: 100%; }}
.bubble {{ background: #fff; border: 1px solid {LINE}; border-radius: 4mm; padding: 3mm 4mm; font-size: 10pt; box-shadow: 0 4px 14px rgba(0,0,0,.06); }}
.bubble small {{ display: block; color: {MUTED}; font: 7.5pt 'JetBrains Mono'; margin-bottom: 1mm; }}
.links {{ font: 9pt 'JetBrains Mono'; color: {INK2}; line-height: 1.8; }}
.links b {{ color: {INK}; font-family: Inter; }}
svg.diagram {{ width: 100%; height: auto; }}
svg text {{ font-family: Inter; }}
svg .h {{ font-size: 15px; font-weight: 700; fill: {INK}; }}
svg .s {{ font-size: 11px; fill: {INK2}; }}
svg .sb {{ font-size: 11px; fill: {BLUE}; font-weight: 600; }}
svg .t {{ font-size: 12px; fill: {INK}; }}
svg .v {{ font-size: 12px; fill: {INK2}; font-weight: 600; }}
svg .vb {{ font-size: 13px; fill: {BLUE}; font-weight: 700; }}
svg .pill rect {{ fill: #fff; stroke: {LINE}; }}
svg .pill text {{ font-size: 12px; fill: {INK}; }}
svg.chart {{ width: 100%; height: auto; }}
.flow {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 3mm; margin-top: 5mm; }}
.step {{ background: #fff; border: 1px solid {LINE}; border-radius: 3.5mm; padding: 3.5mm; font-size: 9pt; color: {INK2}; position: relative; }}
.step b {{ display: block; color: {INK}; font-size: 10.5pt; margin: 2mm 0 1mm; }}
.num {{ display: inline-grid; place-items: center; width: 6.5mm; height: 6.5mm; border-radius: 50%; background: {BLUE}; color: #fff; font-weight: 700; font-size: 9pt; }}
.see {{ display: grid; grid-template-columns: 1.15fr .9fr .75fr; gap: 6mm; align-items: start; }}
.code {{ background: #0f1115; color: #d7dae0; border-radius: 4mm; padding: 4mm 5mm; font: 8.6pt/1.55 'JetBrains Mono'; }}
.code .ref {{ color: #7fb0ff; font-weight: 500; }}
.code .hd {{ color: #f0c674; }}
.col h3 {{ font-size: 11pt; margin: 0 0 2.5mm; }}
.col p {{ font-size: 9.5pt; color: {INK2}; margin: 0 0 2mm; }}
.tools {{ display: flex; flex-wrap: wrap; gap: 1.8mm; margin-bottom: 4mm; }}
.tool {{ font: 8.6pt 'JetBrains Mono'; background: #fff; border: 1px solid {LINE}; border-radius: 2mm; padding: 1mm 2.2mm; }}
.tool.sk {{ border-color: #cfe0f5; background: #f3f7fd; color: {BLUE}; }}
.arrow {{ text-align: center; font-size: 20pt; color: {MUTED}; margin-top: 30mm; }}
.two {{ display: grid; grid-template-columns: 1fr 1fr; gap: 9mm; }}
.card {{ background: #fff; border: 1px solid {LINE}; border-radius: 4mm; padding: 5mm 6mm; }}
.card h3 {{ font-size: 12pt; margin: 0 0 1mm; }}
.card .cap {{ font-size: 9pt; color: {INK2}; margin: 0 0 3mm; }}
.guards {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 3.5mm; }}
.guard {{ background: #fff; border: 1px solid {LINE}; border-radius: 3.5mm; padding: 3mm 3.5mm; font-size: 8.6pt; color: {INK2}; }}
.guard b {{ display: block; color: {INK}; font-size: 10.5pt; margin: 1.5mm 0 1mm; }}
.chk {{ display: inline-grid; place-items: center; width: 6mm; height: 6mm; border-radius: 50%; background: #e6f6ef; color: {AQUA}; font-weight: 700; font-size: 9pt; }}
.big {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 4mm; margin-bottom: 5mm; }}
.big div {{ background: #fff; border: 1px solid {LINE}; border-radius: 4mm; padding: 4mm 5mm; }}
.big b {{ font-size: 24pt; letter-spacing: -.02em; display: block; line-height: 1.05; }}
.big span {{ font-size: 9pt; color: {INK2}; }}
.next {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 4mm; }}
.next div {{ background: #fff; border: 1px solid {LINE}; border-radius: 3.5mm; padding: 3.5mm 4mm; font-size: 9.2pt; color: {INK2}; }}
.next b {{ display: block; color: {INK}; font-size: 10.5pt; margin-bottom: 1mm; }}
.models {{ font-size: 9.5pt; }}
.mrow {{ display: grid; grid-template-columns: 1.6fr .55fr 1.3fr .45fr; gap: 3mm; align-items: center; padding: 1.8mm 2mm; border-radius: 2mm; }}
.mrow + .mrow {{ border-top: 1px solid {LINE}; }}
.mhead {{ color: {MUTED}; font-size: 8pt; text-transform: uppercase; letter-spacing: .06em; }}
.mrow.chosen {{ background: #eef3fb; border-top-color: transparent; }}
.mrow.chosen .mname {{ font-weight: 700; color: {BLUE}; }}
.acc {{ font-weight: 600; font-size: 8.6pt; padding: .4mm 2mm; border-radius: 10mm; text-align: center; }}
.acc.good {{ background: #e6f6ef; color: #0f7a52; }} .acc.ok {{ background: #f3f2ee; color: {INK2}; }} .acc.bad {{ background: #fdecec; color: #b42a29; }}
.mbar {{ height: 2.6mm; background: #f0efeb; border-radius: 2mm; overflow: hidden; }}
.mbar i {{ display: block; height: 100%; background: #b9cfee; border-radius: 2mm; }}
.mrow.chosen .mbar i {{ background: {BLUE}; }}
.mt {{ font-size: 8.6pt; color: {INK2}; text-align: right; }}
.tag {{ display: inline-block; font: 600 7.5pt Inter; letter-spacing: .06em; text-transform: uppercase; padding: .6mm 2mm; border-radius: 10mm; margin-bottom: 1.5mm; }}
.tag.k {{ background: #fdeee7; color: #b2491d; }} .tag.n {{ background: #eef3fb; color: {BLUE}; }}
</style></head><body>

<!-- 1 · cover -->
<section class="page">
  <div class="cover">
    <div>
      <div class="kicker">Cedric take-home · Voice AI &amp; Browser Automation</div>
      <h1>Talk to your CRM.<br><span style="color:{BLUE}">Watch it get done.</span></h1>
      <p class="lede">Ava is a voice agent for EspoCRM. You say what you need; she confirms it; a second AI does it in a real
      browser you can watch — and checks the result before she tells you it's done.</p>
      <div class="kpis" style="grid-template-columns:repeat(2,1fr)">
        <div class="kpi"><b>~1.3 s</b><span>reply time on the last live call (server-side, median)</span></div>
        <div class="kpi"><b>{kpi_bt}</b><span>real CRM jobs done by the browser agent, checked via the API</span></div>
        <div class="kpi"><b>{kpi_llm}</b><span>conversation tests: right action, right values, right moment</span></div>
        <div class="kpi"><b>0</b><span>writes without your spoken “yes” — enforced in code</span></div>
      </div>
      <div class="links"><b>Live demo</b>&nbsp; 34-186-111-176.sslip.io<br><b>Code</b>&nbsp; github.com/Pankaj3112/espo-voice-agent</div>
    </div>
    <div>
      <div class="shot"><div class="bar"><i></i><i></i><i></i><em>demo.us.espocrm.com — the agent's browser, streamed live</em></div><img src="{img(ASSETS / "espo_lead.png")}"></div>
      <div style="display:flex; gap:4mm; margin-top:4mm">
        <div class="bubble" style="flex:1"><small>YOU · example</small>Change Nina's title to Head of Marketing.</div>
        <div class="bubble" style="flex:1.3; border-color:#cfe0f5"><small>AVA</small>I'll set Nina Thomson's title to Head of Marketing, okay?</div>
      </div>
    </div>
  </div>
  <div class="foot"><span>Pankaj Beniwal · Nov 2026</span><span>1 / 5</span></div>
</section>

<!-- 2 · how it works -->
<section class="page">
  <div class="kicker">How it works</div>
  <h2>Two agents, one job each</h2>
  <p class="sub">Ava only talks. Every CRM task goes to a browser agent in plain words and runs in the background,
  so the conversation never stalls while the browser works.</p>
  {ARCH}
  {flow_html()}
  <div class="foot"><span>LiveKit · Deepgram Flux · Gemini 3.1 Flash-Lite on Vertex AI · Fish Audio · Playwright · GCP us-east4</span><span>2 / 5</span></div>
</section>

<!-- 3 · how it sees and acts -->
<section class="page">
  <div class="kicker">Reading and driving the interface</div>
  <h2>It sees the page as text, then acts by number</h2>
  <p class="sub">Each step, the browser agent gets a compact outline of the visible page — every button and field gets a
  number — and replies with one action. Cheaper and faster than screenshots, and it sees values a picture hides.</p>
  <div class="see">
    <div class="col">
      <h3>What's on screen</h3>
      <div class="shot"><img src="{img(ASSETS / "espo_lead.png")}"></div>
      <p style="margin-top:2.5mm">The active control is outlined in purple on the live video, so you can follow along.</p>
    </div>
    <div class="col">
      <h3>What the agent reads</h3>
      <div class="code">{outline_snippet()}</div>
    </div>
    <div class="col">
      <h3>What it can do</h3>
      <p><b>Generic tools</b> — work on any website</p>
      <div class="tools"><span class="tool">look</span><span class="tool">click(3)</span><span class="tool">type_text</span><span class="tool">select_option</span><span class="tool">go_to</span><span class="tool">press_key</span><span class="tool">ask_caller</span><span class="tool">finish</span></div>
      <p><b>Fast skills</b> — scripted for common jobs</p>
      <div class="tools"><span class="tool sk">create_lead</span><span class="tool sk">search_records</span></div>
      <p>Searches are typed into EspoCRM's own search box on screen; a new lead is filled field-by-field and read back.</p>
      <div class="shot" style="margin-top:2mm"><img src="{img(ASSETS / "espo_search.png")}"></div>
    </div>
  </div>
  <div class="foot"><span>Everything that changes data happens through the UI; nothing calls the CRM's API directly.</span><span>3 / 5</span></div>
</section>

<!-- 4 · speed -->
<section class="page">
  <div class="kicker">Speed</div>
  <h2>From ~3 s to ~1.3 s per reply</h2>
  <p class="sub">Measured on real calls, from the moment you stop speaking to Ava's first sound (server-side; add ~0.2 s
  of network from India). Most production voice agents land around 1–1.5 s.</p>
  <div class="two">
    <div class="card"><h3>Every fix, measured</h3><p class="cap">median reply time per build</p>{progress_chart()}
      <h3 style="margin-top:6mm">Where a reply's time goes</h3><p class="cap">a typical turn on the final build</p>{turn_bar()}</div>
    <div class="card"><h3>CRM jobs by the browser agent</h3><p class="cap">16 real jobs on EspoCRM, checked through its API{esc(fail_note)}</p>{tasks_chart()}</div>
  </div>
  <div class="foot"><span>Ava keeps talking while the browser works: she acknowledges at once and gives a short update on long jobs.</span><span>4 / 5</span></div>
</section>

<!-- 5 · quality, choices, next -->
<section class="page">
  <div class="kicker">Quality and choices</div>
  <h2>Tested, guarded, and picked by data</h2>
  <div class="two" style="grid-template-columns: 1fr 1.08fr; align-items:start; margin-top:5mm">
    <div class="card"><h3>Why Gemini 3.1 Flash-Lite</h3><p class="cap">the same conversation tests on every model we tried</p>{models_html()}</div>
    <div>
      <h3 style="font-size:12pt;margin:0 0 3mm">Six guardrails before anything is “done”</h3>
      {guards_html()}
      <div class="next" style="margin-top:5mm">
        <div><span class="tag k">Known gap</span><b>Unusual names</b>Can be misheard; Ava asks to spell them.</div>
        <div><span class="tag k">Known gap</span><b>Multi-step jobs</b>Meetings, deals and conversions take ~15–18 s.</div>
        <div><span class="tag n">Next</span><b>Learned skills</b>Replay a path the agent found once.</div>
      </div>
    </div>
  </div>
  <div class="foot"><span>Reviewed by two independent AI code reviews before submission; confirmed issues fixed. Raw results in evals/results/.</span><span>5 / 5</span></div>
</section>
</body></html>"""


async def main() -> None:
    from playwright.async_api import async_playwright

    out_html = ROOT / "docs" / "technical-notes.html"
    out_html.write_text(build())
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        p = await b.new_page()
        await p.goto(out_html.as_uri(), wait_until="networkidle")
        await p.wait_for_timeout(800)
        await p.pdf(path=str(ROOT / "docs" / "technical-notes.pdf"), width="297mm", height="210mm", print_background=True)
        await b.close()
    print("wrote docs/technical-notes.pdf")


if __name__ == "__main__":
    asyncio.run(main())
