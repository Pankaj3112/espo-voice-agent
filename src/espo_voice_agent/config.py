"""Settings. Secrets live outside the repo (default ~/.config/espo-voice-agent/.env)."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

SECRETS_FILE = Path(os.environ.get("ESPO_AGENT_ENV", "~/.config/espo-voice-agent/.env")).expanduser()
load_dotenv(SECRETS_FILE)
load_dotenv()  # optional local .env for non-secret overrides

ESPO_BASE_URL = os.environ.get("ESPO_BASE_URL", "https://demo.us.espocrm.com/")
AGENT_NAME = os.environ.get("AGENT_NAME", "espo-intake")

# Chosen by evals/llm_eval.py (25 cases x2, run from the server). Gemini 3.1 Flash-Lite on Vertex AI:
# 50/50, ~0.63 s median to first token, thinking off. 3.6 Flash was retired on Vertex on 2026-11-19.
LLM_BACKEND = os.environ.get("LLM_BACKEND", "vertex")  # "vertex" | "openrouter"
LLM_MODEL = os.environ.get("LLM_MODEL", "gemini-3.1-flash-lite" if LLM_BACKEND == "vertex" else "google/gemini-3.6-flash")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "google-ai-studio")  # OpenRouter upstream, pinned
LLM_REASONING = os.environ.get("LLM_REASONING", "minimal")  # no hidden thinking before the first token

# "flux": Deepgram Flux does STT and end-of-turn detection in one model (no local turn-detector).
# "nova": Nova-3 STT + Silero VAD + LiveKit's multilingual turn-detector model (the earlier setup).
GCP_PROJECT = os.environ.get("GCP_PROJECT", "espo-voice-agent-1127")
GCP_LOCATION = os.environ.get("GCP_LOCATION", "global")

# Browser agent (second LLM that operates the UI). Accuracy matters more than first-token speed here.
# evals/browser_tasks.py: 3.1 Flash-Lite 6/6 at 9.0 s median vs 3.8 Flash 6/6 at 16.5 s.
BROWSER_LLM_MODEL = os.environ.get("BROWSER_LLM_MODEL", "gemini-3.1-flash-lite")
BROWSER_LLM_THINKING = os.environ.get("BROWSER_LLM_THINKING", "minimal")
BROWSER_TASK_TIMEOUT_S = float(os.environ.get("BROWSER_TASK_TIMEOUT_S", "150"))
BROWSER_TASK_MAX_STEPS = int(os.environ.get("BROWSER_TASK_MAX_STEPS", "30"))

STT_MODE = os.environ.get("STT_MODE", "flux")
STT_MODEL = os.environ.get("STT_MODEL", "flux-general-en" if STT_MODE == "flux" else "nova-3")
FLUX_EOT_THRESHOLD = float(os.environ.get("FLUX_EOT_THRESHOLD", "0.7"))
FLUX_EAGER_EOT_THRESHOLD = float(os.environ.get("FLUX_EAGER_EOT_THRESHOLD", "0.5"))  # starts the LLM early
FLUX_EOT_TIMEOUT_MS = int(os.environ.get("FLUX_EOT_TIMEOUT_MS", "2000"))
# "fish": Fish Audio S2.1 Pro via LiveKit Inference (picked by ear from evals/voice_samples.py; ~0.5 s to first
# audio from the server), with Deepgram Aura-2 as automatic fallback. "elevenlabs" / "deepgram" also work.
TTS_PROVIDER = os.environ.get("TTS_PROVIDER", "fish")
TTS_MODEL = os.environ.get("TTS_MODEL", {"fish": "fishaudio/s2.1-pro", "elevenlabs": "eleven_flash_v2_5"}.get(TTS_PROVIDER, "aura-2-andromeda-en"))
TTS_FALLBACK_MODEL = os.environ.get("TTS_FALLBACK_MODEL", "aura-2-andromeda-en")
TTS_VOICE_ID = os.environ.get("TTS_VOICE_ID", "hpp4J3VqNfWAUOO0d1Us")  # "Bella", premade

MAX_CALL_SECONDS = int(os.environ.get("MAX_CALL_SECONDS", "240"))
MAX_CONCURRENT_CALLS = int(os.environ.get("MAX_CONCURRENT_CALLS", "2"))
SCREEN_FPS = float(os.environ.get("SCREEN_FPS", "6"))

LOG_DIR = Path(os.environ.get("LOG_DIR", Path(__file__).resolve().parents[2] / "logs"))
