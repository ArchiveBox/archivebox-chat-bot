"""Meta's official WhatsApp Agent Platform v1 (2026-08-25 developer manual).

https://www.whatsapp.com/developer/WhatsApp-Agent-Platform-Developer-Manual.pdf
The creator-only agent API uses outbound long polling; no webhook or Web session.
"""

import asyncio
import base64
import binascii
import contextlib
import fcntl
import hashlib
import json
import os
import re
import time
from collections import defaultdict, deque
from pathlib import Path

import httpx

BASE_URL = "https://api.whatsapp.com/agent/v1"


class WhatsAppAgentTransport:
    def __init__(self, options: dict, account_id: str, data_dir: Path, emit):
        self.options, self.account_id, self.emit = options, account_id, emit
        token = options.get("api_key", "")
        if not token:
            raise ValueError("WhatsApp agent API key is required")
        fingerprint = hashlib.sha256(token.encode()).hexdigest()[:24]
        # One upstream poller per key, even when multiple configured roles share it.
        self.path = Path(data_dir).parent / f"whatsapp-agent-{fingerprint}.json"
        # The upstream cursor belongs to the key, not a UI connection or bot role.
        # Preserve the furthest acknowledged cursor from previous per-role files.
        if not self.path.exists():
            previous = [json.loads(path.read_text()) for path in self.path.parent.glob(f"*/{self.path.name}")]
            if previous:
                self.state = max(previous, key=lambda value: value.get("offset", 0))
                self._save()
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {"offset": 0, "contacts": {}}
        self.client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={"Authorization": "Bearer " + token},
            timeout=httpx.Timeout(60, connect=15),
            follow_redirects=False,
        )
        self.task = None
        self.first_page = None
        self.error = ""
        self.lock_file = None
        self.send_lock = asyncio.Lock()
        self.poll_lock = asyncio.Lock()
        self.budgets = defaultdict(deque)

    def _lock(self):
        if self.lock_file:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        handle = os.fdopen(descriptor, "w")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise RuntimeError(
                "This WhatsApp agent key already has a local poller; use one active role per key"
            ) from None
        self.lock_file = handle

    def _save(self):
        temporary = self.path.with_suffix(".tmp")
        with os.fdopen(os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w") as stream:
            json.dump(self.state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    async def _pace(self, lane):
        timestamps = self.budgets[lane]
        limit = 14 if lane == "updates" else 12
        while timestamps and time.monotonic() - timestamps[0] >= 60:
            timestamps.popleft()
        if len(timestamps) >= limit:
            await asyncio.sleep(max(0, 60 - (time.monotonic() - timestamps[0])))
            while timestamps and time.monotonic() - timestamps[0] >= 60:
                timestamps.popleft()
        timestamps.append(time.monotonic())

    async def _request(self, method, path, **kwargs):
        await self._pace(path)
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.TransportError:
            action = (
                "delivery may have completed; inspect the chat before retrying"
                if method == "POST"
                else "reconnect to resume"
            )
            raise RuntimeError(f"WhatsApp agent network failure; {action}") from None
        if response.status_code == 204:
            return None
        try:
            payload = response.json()
        except ValueError:
            raise RuntimeError(
                "WhatsApp agent returned an invalid response; delivery status may be uncertain"
            ) from None
        if not response.is_success:
            code = payload.get("error", {}).get("code")
            if response.status_code == 409:
                raise RuntimeError(
                    "WhatsApp agent poll conflict: another process is using this key; stop the duplicate poller"
                )
            if response.status_code == 401 or (response.status_code == 400 and code == 100 and path == "updates"):
                raise RuntimeError("WhatsApp agent authentication failed; check or regenerate the API key")
            if response.status_code == 429:
                raise RuntimeError("WhatsApp agent rate limit reached; wait before trying again")
            if response.status_code >= 500 and not (response.status_code == 503 and code == 131016):
                raise RuntimeError(
                    "WhatsApp agent server failure; delivery may have completed. Inspect the chat before retrying"
                )
            raise RuntimeError(f"WhatsApp agent request rejected (HTTP {response.status_code}, code {code})")
        return payload

    async def _poll(self, timeout):
        async with self.poll_lock:
            return await self._request(
                "GET", "updates", params={"offset": self.state["offset"], "limit": 100, "timeout": timeout}
            )

    def _identity(self, page):
        if page.get("object") != "whatsapp_agent_platform" or len(page.get("entry", [])) != 1:
            raise RuntimeError("Unexpected WhatsApp agent response shape")
        identity = "agent:" + str(page["entry"][0]["id"])
        if self.state.get("id") and self.state["id"] != identity:
            raise RuntimeError("WhatsApp agent identity changed; verify the configured key")
        self.state["id"] = identity

    async def check(self):
        self._lock()
        if self.error:
            raise RuntimeError(self.error)
        if not self.task and self.first_page is None:
            self.first_page = await self._poll(0)
            if self.first_page:
                self._identity(self.first_page)
        return {
            "id": self.state.get("id", self.account_id),
            "name": self.options.get("name", "WhatsApp agent"),
            "capabilities": {"reactions": False, "media": True, "threads": True, "groups": False},
        }

    async def start(self):
        if self.task:
            return
        await self.check()
        # check() holds its page for ingestion; no poll ever advances past unaccepted messages.
        if self.first_page:
            await self._ingest(self.first_page)
            self.first_page = None
        self.task = asyncio.create_task(self._run())

    async def _ingest(self, page):
        self._identity(page)
        for entry in page["entry"]:
            for change in entry.get("changes", []):
                if change.get("field") != "messages":
                    continue
                value = change["value"]
                for contact in value.get("contacts", []):
                    sender = contact["wa_id"]
                    self.state["contacts"][sender] = contact.get("profile", {}).get("name") or self.state[
                        "contacts"
                    ].get(sender, "WhatsApp user")
                for message in value.get("messages", []):
                    sender = message["from"]
                    if not sender.startswith("user:") or message["type"] == "reaction":
                        continue
                    content = message.get(message["type"], {})
                    text = content.get("body", "") if message["type"] == "text" else content.get("caption", "")
                    if message["type"] != "text":
                        text += f"\n[WhatsApp {message['type']} attachment; content not downloaded]"
                    self.state["contacts"].setdefault(sender, "WhatsApp user")
                    await self.emit(
                        {
                            "id": message["id"],
                            "channel": sender,
                            "user": sender,
                            "user_name": self.state["contacts"][sender],
                            "text": text,
                            "thread": message.get("context", {}).get("id", ""),
                            "is_dm": True,
                            "is_mention": False,
                            "is_bot": False,
                        }
                    )
        offset = page.get("next_offset")
        if not isinstance(offset, int) or offset < self.state["offset"]:
            raise RuntimeError("WhatsApp agent returned an invalid update cursor")
        self.state["offset"] = offset
        self._save()

    async def _run(self):
        try:
            while True:
                page = await self._poll(25)
                if page:
                    await self._ingest(page)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - boundary reports safe errors without upstream payloads
            self.error = (
                str(exc)
                if isinstance(exc, RuntimeError)
                else "WhatsApp agent ingestion failed; reconnect after checking the durable inbox"
            )

    async def _send_one(self, channel, kind, content, thread):
        payload = {"messaging_product": "whatsapp", "to": channel, "type": kind, kind: content}
        if thread:
            payload["context"] = {"message_id": thread}
        result = await self._request("POST", "messages", json=payload)
        try:
            return result["messages"][0]["id"]
        except (KeyError, IndexError, TypeError):
            raise RuntimeError("WhatsApp agent send returned no message ID; inspect the chat before retrying") from None

    async def send(self, channel: str, text: str, thread: str = "", media: list[dict] | None = None):
        if not re.fullmatch(r"user:[^\s]+", channel):
            raise ValueError("WhatsApp agents can only send to an opaque user ID from an inbound message")
        if len(text) > 4096:
            raise ValueError("WhatsApp agent text is limited to 4096 characters")
        attachments = []
        for item in media or []:
            mime = item.get("mimetype", "")
            if mime not in {"image/png", "image/jpeg"}:
                raise ValueError("WhatsApp agent previews currently support PNG and JPEG uploads only")
            try:
                data = base64.b64decode(item.get("data", ""), validate=True)
            except (ValueError, binascii.Error):
                raise ValueError("Invalid base64 WhatsApp image") from None
            if not data or len(data) > 5 * 1024 * 1024:
                raise ValueError("WhatsApp agent images must be nonempty and at most 5 MB")
            filename = Path(item.get("filename", "preview.png")).name
            attachments.append((filename, data, mime))
        if not text and not attachments:
            raise ValueError("A WhatsApp agent message requires text or supported media")
        async with self.send_lock:
            uploaded = []
            for filename, data, mime in attachments:
                result = await self._request(
                    "POST",
                    "media",
                    data={"messaging_product": "whatsapp", "type": mime},
                    files={"file": (filename, data, mime)},
                )
                uploaded.append(result["id"])
            sent = []
            try:
                if text:
                    sent.append(await self._send_one(channel, "text", {"body": text}, thread))
                for media_id in uploaded:
                    sent.append(await self._send_one(channel, "image", {"id": media_id}, thread))
            except Exception:
                if sent:
                    raise RuntimeError(
                        "WhatsApp message was partially delivered; inspect the chat before retrying"
                    ) from None
                raise
            return sent[-1]

    async def react(self, channel: str, message_id: str, status: str):
        raise NotImplementedError("WhatsApp Agent Platform supports receiving reactions, not sending them")

    async def join(self, channel: str):
        for item in await self.channels():
            if item["id"] == channel:
                return item
        raise ValueError("WhatsApp agents cannot join groups; first send a DM to the agent from its creator account")

    async def channels(self):
        return [{"id": sender, "name": name, "is_dm": True} for sender, name in self.state["contacts"].items()]

    async def close(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        await self.client.aclose()
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None
