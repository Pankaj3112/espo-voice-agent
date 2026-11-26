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

LLM_MODEL = os.environ.get("LLM_MODEL", "openai/gpt-6-luna")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "openai")  # OpenRouter upstream, pinned
LLM_REASONING = os.environ.get("LLM_REASONING", "none")

STT_MODEL = os.environ.get("STT_MODEL", "nova-3")
TTS_MODEL = os.environ.get("TTS_MODEL", "eleven_flash_v2_5")
TTS_VOICE_ID = os.environ.get("TTS_VOICE_ID", "hpp4J3VqNfWAUOO0d1Us")  # "Bella", premade

MAX_CALL_SECONDS = int(os.environ.get("MAX_CALL_SECONDS", "240"))
MAX_CONCURRENT_CALLS = int(os.environ.get("MAX_CONCURRENT_CALLS", "2"))
SCREEN_FPS = float(os.environ.get("SCREEN_FPS", "6"))

LOG_DIR = Path(os.environ.get("LOG_DIR", Path(__file__).resolve().parents[2] / "logs"))
