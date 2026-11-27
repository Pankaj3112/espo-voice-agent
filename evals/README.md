# Evals

| Script | What it does | Final result file |
|---|---|---|
| `llm_eval.py` | Ava's brain on 38 ASR-style dialogues (`llm_cases.json`), production prompt, tools and settings; scores the action, the values in the hand-off, and timing (no write before a clear yes). | `results/llm_eval_final.json`: 76/76 |
| `browser_tasks.py` | The browser agent on 16 real jobs in the EspoCRM demo. Each job gets fresh test data over REST, is verified exactly over REST, then cleaned up. | `results/browser_tasks_final.json`: 16/16 |
| `sim_call.py` | Full voice loop: an LLM caller speaks (TTS) into the LiveKit room; outcome checked in the CRM. Optional background noise. | `results/sim_calls_final.jsonl`: 2/4 (see README) |
| `voice_samples.py` | Renders the same line in several TTS voices for picking by ear. | `results/voices/index.json` |
| `ttft_interleaved.py` | Time-to-first-token benchmark across models, interleaved to cancel out network drift. | `results/history/ttft_interleaved.json` |

`results/history/` keeps every earlier run, in order:

- `llm_eval_round1.json`, `llm_eval.json`: model selection via OpenRouter (GPT-6 Luna, Claude Haiku 5.5/4.5, Gemini 3.6 Flash, 3.5 Flash-Lite).
- `llm_eval_37.json`, `llm_eval_vertex*.json`: Gemini 3.7/3.8/3.5 on Vertex (3.6 Flash was retired on Vertex).
- `llm_eval_v2` … `v6`: Gemini 3.1 Flash-Lite as the case set grew (25 → 38 cases).
- `browser_tasks_38flash.json`, `browser_tasks_lite.json`: browser-model choice (3.8 Flash vs 3.1 Flash-Lite).
- `browser_tasks_v3/v5.json`, `bt_verify.json`: browser agent as tasks were added and verification was enforced.
- `sim_calls*.jsonl`: earlier simulated-call runs (home server, first cloud VM).
