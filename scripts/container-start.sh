#!/usr/bin/env bash
# Runs the LiveKit agent worker and the web server; if either exits, the container exits and Docker restarts it.
set -uo pipefail
python -m espo_voice_agent.agent start &
uvicorn espo_voice_agent.web:app --host 0.0.0.0 --port "${PORT:-8790}" --proxy-headers --forwarded-allow-ips='*' &
wait -n
exit 1
