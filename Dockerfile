FROM python:3.13-slim-trixie
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY archivebox_chat_bot ./archivebox_chat_bot
RUN uv sync --frozen --no-dev && useradd --uid 1000 --create-home bridge && mkdir /data && chown bridge:bridge /data
ENV PATH="/app/.venv/bin:$PATH" DATA_DIR=/data HOST=0.0.0.0 PORT=8001 PYTHONUNBUFFERED=1
USER bridge
EXPOSE 8001
HEALTHCHECK --interval=30s --timeout=5s CMD ["uv", "run", "--no-sync", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/healthz')"]
CMD ["archivebox-chat-bot"]
