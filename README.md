# espo-voice-agent

A voice agent that adds leads to a real CRM by driving its web UI. You talk to it in the browser; it
listens, confirms the details back, then fills in and saves the Lead form in the public
[EspoCRM demo](https://demo.us.espocrm.com/) with Playwright, while you watch that browser live.

**Live demo:** <https://homeserver.tailc5973b.ts.net:10000/>  ·  **Technical notes (PDF):** [`docs/technical-notes.pdf`](docs/technical-notes.pdf)

> Try: *"Add Jane Doe from Acme Robotics, phone 415 555 0142, email jane dot doe at acme dot com."*
> Ava reads it back, you say "yes", and the lead appears in EspoCRM (link shown when saved).

## How it works

```
 caller's browser ──WebRTC──► LiveKit Cloud ◄──── agent worker (this repo, Python)
   mic, transcript,                               ├─ Deepgram Nova-3 (streaming STT)
   live view of the                               ├─ Silero VAD + LiveKit turn detector
   agent's browser                                ├─ LLM via OpenRouter (tool calling)
                                                  ├─ ElevenLabs Flash v2.5 (TTS)
                                                  └─ Playwright Chromium ──► demo.us.espocrm.com
                                                       └─ CDP screencast ──► video track back to caller
```

- **The LLM never clicks pixels.** It has two tools, `create_lead` and `find_lead`. Each is a deterministic
  Playwright routine that targets EspoCRM's stable `data-name` field attributes and fills the form the way a
  person would (visible typing, a purple outline on the active field).
- **Every write is verified.** After saving, the agent reads the record back from the detail view and compares
  it with what the caller said. It only says "added" when they match.
- **Writes need an explicit yes.** Besides the prompt, `create_lead` is guarded in code: the caller's latest
  utterance must be a clear yes with no correction in it (`is_confirmation` in `agent.py`), else nothing is saved.
- **Duplicates are checked first** through the logged-in app's own AJAX layer (~0.4 s), and the caller decides.
- **The caller is never left in silence**: a short "one moment" line plays while the browser works.

## Run it locally

Requires Python 3.12, [uv](https://docs.astral.sh/uv/), and accounts for LiveKit Cloud, Deepgram, ElevenLabs and
OpenRouter (all have free tiers; OpenRouter needs a few dollars of credit).

```bash
uv sync
uv run playwright install chromium
uv run python -m espo_voice_agent.agent download-files   # VAD + turn-detector weights

# secrets live outside the repo (override the path with ESPO_AGENT_ENV)
mkdir -p ~/.config/espo-voice-agent && cp .env.example ~/.config/espo-voice-agent/.env
chmod 600 ~/.config/espo-voice-agent/.env   # then fill in the keys

scripts/dev.sh start        # agent worker + web server on http://127.0.0.1:8790
```

## Evals

| Eval | What it checks | Command |
|---|---|---|
| Unit tests | confirmation guard, read-back comparison | `uv run pytest` |
| LLM tool-use eval | right tool, right fields, right moment, on 25 ASR-style transcripts; time-to-first-token | `uv run python evals/llm_eval.py --models ...` |
| Simulated calls | full voice loop: an LLM caller speaks (Deepgram Aura) into the room, the agent hears audio only; outcome verified via EspoCRM's REST API | `uv run python evals/sim_call.py --all` |

Results and what I learned from them are in the technical notes.

## Repo layout

```
src/espo_voice_agent/
  agent.py      LiveKit agent, tools, confirmation guard, per-call logs
  espo.py       EspoCRM browser driver (Playwright): search, duplicate check, create + verify
  screen.py     CDP screencast -> LiveKit video track
  prompt.py     system prompt and greeting
  web.py        demo page + token endpoint (rate limits, concurrency cap)
  static/       the demo page
evals/          LLM eval, simulated-call eval, results
tests/          unit tests
scripts/dev.sh  start/stop locally
```
