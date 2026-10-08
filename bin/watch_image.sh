#!/usr/bin/env bash
# Follow the CI-published main image using the same registry polling as ArchiveBox.
set -Eeuo pipefail
DEPLOY_PATH="${DEPLOY_PATH:-/opt/archivebox.demo}"
DEPLOY_IMAGE="${DEPLOY_IMAGE:-archivebox/archivebox-chat-bot:latest}"
DEPLOY_INTERVAL="${DEPLOY_INTERVAL:-60}"
cd "$DEPLOY_PATH"
exec 9>.chatbot-deploy.lock
flock -n 9 || exit 0
COMPOSE=(docker compose -p archiveboxdemo -f docker-compose.yml -f .archivebox-deploy.override.yml)
STATE_FILE=.chatbot-image.digest
OVERRIDE_FILE=.chatbot-deploy.override.yml
while :; do
    digest="$(docker buildx imagetools inspect "$DEPLOY_IMAGE" | awk '/^Digest:/ && !found {digest=$2; found=1} END {if (found) print digest}')"
    [[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]]
    previous="$(cat "$STATE_FILE" 2>/dev/null || true)"
    if [[ "$digest" != "$previous" ]]; then
        active=0
        if [[ -f archivebox-chat-bot/bridge.sqlite3 ]]; then
            active="$(sqlite3 -readonly archivebox-chat-bot/bridge.sqlite3 "SELECT COUNT(*) FROM jobs WHERE state='running';")"
        fi
        if [[ "$active" != 0 ]]; then
            echo "[=] Waiting for $active active chatbot operation(s) before updating"
        else
            image="${DEPLOY_IMAGE}@${digest}"
            echo "[+] Pulling $image"
            docker pull "$image"
            # Pin the inspected digest so a concurrent publish cannot change this deployment.
            printf 'services:\n  archivebox-chat-bot:\n    image: %s\n' "$image" > "$OVERRIDE_FILE.tmp"
            mv "$OVERRIDE_FILE.tmp" "$OVERRIDE_FILE"
            "${COMPOSE[@]}" -f "$OVERRIDE_FILE" up -d --no-deps --pull never --wait --wait-timeout 90 archivebox-chat-bot
            curl --fail --silent --show-error --max-time 10 http://127.0.0.1:5798/healthz
            printf '%s\n' "$digest" > "$STATE_FILE.tmp"
            mv "$STATE_FILE.tmp" "$STATE_FILE"
            echo "[+] Chatbot healthy at $digest"
        fi
    fi
    [[ "${WATCH_ONCE:-0}" == 1 ]] && break
    sleep "$DEPLOY_INTERVAL"
done
