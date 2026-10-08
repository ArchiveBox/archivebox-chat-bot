FROM node:22-trixie-slim AS connectors
WORKDIR /connectors
COPY connectors/package.json connectors/package-lock.json ./
RUN npm ci
COPY connectors/src ./src
COPY connectors/tsconfig.json connectors/build.mjs ./
RUN npm run build && npm prune --omit=dev

FROM python:3.13-slim-trixie
COPY --from=ghcr.io/astral-sh/uv:0.11.29 /uv /uvx /bin/
COPY --from=connectors /usr/local/bin/node /usr/local/bin/node
RUN apt-get update && apt-get install -y --no-install-recommends openssh-client gosu && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=connectors /connectors ./connectors
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY archivebox_chat_bot ./archivebox_chat_bot
RUN uv sync --frozen --no-dev && useradd --uid 1000 --create-home bridge && mkdir /data && chown bridge:bridge /data
ENV PATH="/app/.venv/bin:$PATH" DATA_DIR=/data HOST=0.0.0.0 PORT=5798 PYTHONUNBUFFERED=1
EXPOSE 5798
HEALTHCHECK --interval=30s --timeout=5s CMD ["uv", "run", "--no-sync", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5798/healthz')"]
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["archivebox-chat-bot"]
