#!/usr/bin/env bash
# Start/stop the agent worker and web server locally (pid files in logs/).
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs
stop() { for n in agent web; do [[ -f logs/$n.pid ]] && kill "$(cat logs/$n.pid)" 2>/dev/null || true; rm -f logs/$n.pid; done; }
case "${1:-start}" in
  stop) stop ;;
  start|restart)
    stop; sleep 1
    nohup uv run python -m espo_voice_agent.agent dev > logs/agent-dev.log 2>&1 & echo $! > logs/agent.pid
    nohup uv run uvicorn espo_voice_agent.web:app --host 127.0.0.1 --port "${PORT:-8790}" --proxy-headers > logs/web.log 2>&1 & echo $! > logs/web.pid
    for _ in $(seq 1 60); do grep -q "registered worker" logs/agent-dev.log 2>/dev/null && break; sleep 1; done
    grep -q "registered worker" logs/agent-dev.log && echo "agent registered; web on :${PORT:-8790}" || { echo "agent failed to register"; tail -20 logs/agent-dev.log; exit 1; } ;;
esac
