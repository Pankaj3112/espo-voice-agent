# Agent worker + web page in one image. Base image ships Chromium matching Playwright 1.65.2.
FROM mcr.microsoft.com/playwright/python:v1.65.2-noble

ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /usr/local/bin/uv
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY README.md ./
COPY src ./src
COPY scripts/container-start.sh ./scripts/
RUN uv sync --frozen --no-dev \
 && /opt/venv/bin/python -m espo_voice_agent.agent download-files

ENV PATH=/opt/venv/bin:$PATH LOG_DIR=/app/logs PORT=8790
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/healthz', timeout=4)"
CMD ["/app/scripts/container-start.sh"]
