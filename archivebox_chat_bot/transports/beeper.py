"""Beeper Client API for Desktop and Server, verified against server 4.3.176 OpenAPI.

https://developers.beeper.com/desktop-api-reference/
REST reconciliation is authoritative: the experimental WebSocket can skip events.
"""

import asyncio
import base64
import contextlib
import fnmatch
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

import httpx


class BeeperTransport:
    def __init__(self, options: dict, account_id: str, data_dir: Path, emit):
        self.options, self.account_id, self.emit = options, account_id, emit
        self.base_url = options.get("base_url", "http://127.0.0.1:23373").rstrip("/")
        url = urlsplit(self.base_url)
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ValueError("Beeper base URL must be an HTTP(S) endpoint without embedded credentials")
        if url.query or url.fragment:
            raise ValueError("Beeper base URL cannot contain a query or fragment")
        self.network_account = options.get("account_id", "")
        if not self.network_account:
            raise ValueError("Select a connected Beeper account for this bot role")
        self.chat_ids = frozenset(options.get("chat_ids") or [])
        headers = {"Authorization": "Bearer " + options["access_token"]} if options.get("access_token") else {}
        self.client = httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=30, follow_redirects=False)
        scope = hashlib.sha256(json.dumps([self.base_url, self.network_account]).encode()).hexdigest()[:24]
        self.path = Path(data_dir) / f"beeper-{scope}.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {"cursors": {}}
        self.identity = None
        self.error = ""
        self.task = None
        self.send_lock = asyncio.Lock()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with os.fdopen(os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w") as stream:
            json.dump(self.state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    async def _request(self, method, path, *, allow_missing=False, **kwargs):
        try:
            response = await self.client.request(method, path, **kwargs)
        except httpx.TransportError:
            raise RuntimeError("Beeper is unreachable; outbound delivery may be uncertain") from None
        if response.status_code in {401, 403}:
            raise RuntimeError("Beeper authentication required; sign in and approve an API connection")
        if allow_missing and response.status_code == 404:
            return None
        if not response.is_success:
            # Provider bodies can include tokens, messages, or internal bridge details.
            raise RuntimeError(f"Beeper request failed (HTTP {response.status_code}); check the connected account")
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError:
            raise RuntimeError("Beeper returned an invalid response") from None

    @staticmethod
    def _part(value):
        return quote(str(value), safe="")

    async def check(self):
        if self.error:
            raise RuntimeError(self.error)
        setup = await self._request("GET", "/v1/app/setup")
        if setup.get("state") == "needs-login":
            raise RuntimeError("Beeper needs sign-in; complete email sign-in and device verification on this server")
        account = await self._request("GET", "/v1/accounts/" + self._part(self.network_account))
        if account.get("accountID") != self.network_account or not account.get("user", {}).get("id"):
            raise RuntimeError("Beeper did not return the selected account identity")
        if account.get("status") != "connected":
            raise RuntimeError("Beeper account is not connected; finish login or reconnect it in Beeper")
        identity = account["user"]["id"]
        if self.state.get("identity") not in {None, identity}:
            raise RuntimeError(
                "Beeper account identity changed; configure a new connection to preserve history isolation"
            )
        self.state["identity"] = identity
        self._save()
        self.identity = account
        # Account capabilities are optional. Selected chats report the actual
        # network/conversation capabilities; send/react validate the destination again.
        capabilities = [(await self._chat(channel)).get("capabilities") or {} for channel in sorted(self.chat_ids)]
        if not capabilities:
            capabilities = [account.get("capabilities") or {}]
        return {
            "id": "beeper:"
            + hashlib.sha256(json.dumps([self.base_url, self.network_account, identity]).encode()).hexdigest(),
            "name": account["user"].get("fullName") or account["user"].get("username") or account.get("network"),
            "capabilities": {
                "reactions": any(caps.get("reaction", 0) > 0 for caps in capabilities),
                "media": any(
                    level > 0
                    for caps in capabilities
                    for attachment in caps.get("attachments", {}).values()
                    for level in attachment.get("mimeTypes", {}).values()
                ),
                "threads": any(caps.get("reply", 0) > 0 for caps in capabilities),
            },
        }

    async def _chat(self, channel, *, selected=True):
        if selected and channel not in self.chat_ids:
            raise ValueError("Select this Beeper conversation in the connection settings first")
        chat = await self._request("GET", "/v1/chats/" + self._part(channel))
        if chat.get("id") != channel or chat.get("accountID") != self.network_account or chat.get("merge"):
            raise ValueError("Choose an unmerged chat belonging to this bot's Beeper account")
        return chat

    async def _chats(self):
        params = {"accountIDs": self.network_account, "limit": 200}
        result, visited = {}, set()
        while True:
            page = await self._request("GET", "/v1/chats", params=params)
            for chat in page["items"]:
                # Merged chats contain no messages. Resolve only their selected account's members.
                if chat.get("merge"):
                    for member in chat["merge"]["chatIDs"]:
                        item = await self._request("GET", "/v1/chats/" + self._part(member))
                        if item.get("accountID") == self.network_account and not item.get("merge"):
                            result[item["id"]] = item
                elif chat.get("accountID") == self.network_account:
                    result[chat["id"]] = chat
            if not page["hasMore"]:
                return list(result.values())
            cursor = page.get("oldestCursor")
            if not cursor or cursor in visited:
                raise RuntimeError("Beeper chat pagination stopped advancing")
            visited.add(cursor)
            params.update(cursor=cursor, direction="before")

    async def channels(self):
        return [{"id": c["id"], "name": c["title"], "is_dm": c["type"] == "single"} for c in await self._chats()]

    async def join(self, channel):
        chat = await self._chat(channel, selected=False)
        return {"id": chat["id"], "name": chat["title"], "is_dm": chat["type"] == "single"}

    def _message(self, message, chat):
        if message.get("accountID") != self.network_account or message.get("chatID") != chat["id"]:
            raise RuntimeError("Beeper message did not match the selected account and chat")
        participants = {p["id"]: p for p in chat.get("participants", {}).get("items", [])}
        sender = participants.get(message["senderID"], {})
        mentions = message.get("mentions") or []
        # A recognized network name disambiguates multiprotocol bridges such
        # as Meta. Fall back to bridge type when the display name is unknown;
        # the connection still routes through Beeper.
        aliases = {
            "slackgo": "slack",
            "discordgo": "discord",
            "meta": "messenger",
            "facebookmessenger": "messenger",
            "facebook": "messenger",
            "heisenbridge": "irc",
        }
        networks = {
            "slack",
            "zulip",
            "telegram",
            "whatsapp",
            "irc",
            "imessage",
            "messenger",
            "matrix",
            "discord",
            "signal",
            "instagram",
            "twitter",
            "linkedin",
            "googlechat",
            "googlemessages",
            "googlevoice",
            "bluesky",
            "line",
        }
        source_platform = "beeper"
        for value in (self.identity.get("network"), self.identity.get("bridge", {}).get("type")):
            normalized = str(value or "").lower().replace(" ", "").replace("-", "").replace("_", "")
            normalized = aliases.get(normalized, normalized)
            if normalized in networks:
                source_platform = normalized
                break
        return {
            "id": message["id"],
            "channel": chat["id"],
            "source_platform": source_platform,
            **({"channel_name": chat["title"]} if chat["type"] == "group" and chat.get("title") else {}),
            "user": message["senderID"],
            "user_name": message.get("senderName")
            or sender.get("fullName")
            or sender.get("username")
            or message["senderID"],
            "text": message.get("text") or "",
            # linkedMessageID is an ordinary reply, not a conversation/topic identifier.
            "thread": "",
            "is_dm": chat["type"] == "single",
            "is_mention": self.identity["user"]["id"] in mentions or "@room" in mentions,
            "is_bot": bool(sender.get("isNetworkBot") or message.get("isSender")),
        }

    async def start(self):
        if self.task:
            return
        await self.check()
        selected = self.state.setdefault("selected", {})
        for channel in set(selected) - self.chat_ids:
            selected.pop(channel)
            self.state["cursors"].pop(channel, None)
        for channel in self.chat_ids:
            selected.setdefault(channel, datetime.now(UTC).isoformat())
        self._save()
        await self._reconcile()
        self.task = asyncio.create_task(self._run())

    async def _reconcile(self):
        for channel in sorted(self.chat_ids):
            chat = await self._chat(channel)
            since = datetime.fromisoformat(self.state["selected"][channel])
            cursor = self.state["cursors"].get(channel)
            params = {"cursor": cursor, "direction": "after"} if cursor else {}
            pages, visited, newest = [], set(), None
            while True:
                page = await self._request("GET", f"/v1/chats/{self._part(channel)}/messages", params=params)
                if newest is None:
                    newest = page.get("newestCursor")
                pages.append(page)
                if page["hasMore"] and not page["items"]:
                    raise RuntimeError("Beeper returned an empty page with more messages; cursor was not advanced")
                if not page["hasMore"] or not page["items"]:
                    break
                if not cursor and min(datetime.fromisoformat(m["timestamp"]) for m in page["items"]) < since:
                    break
                next_cursor = page.get("newestCursor" if cursor else "oldestCursor")
                if not next_cursor or next_cursor in visited or next_cursor == params.get("cursor"):
                    raise RuntimeError("Beeper message pagination stopped advancing")
                visited.add(next_cursor)
                params = {"cursor": next_cursor, "direction": "after" if cursor else "before"}
            # Fresh chats page backwards; existing chats catch up forwards from their durable cursor.
            messages = {m["id"]: m for p in pages for m in p["items"]}
            for message in sorted(messages.values(), key=lambda m: (m["timestamp"], m["sortKey"])):
                if datetime.fromisoformat(message["timestamp"]) < since:
                    continue
                if message.get("isSender") or message["senderID"] == self.identity["user"]["id"]:
                    continue
                if message.get("isHidden") or message.get("isDeleted") or message.get("type") in {"REACTION", "NOTICE"}:
                    continue
                await self.emit(self._message(message, chat))
            if cursor:
                newest = next((p["newestCursor"] for p in reversed(pages) if p.get("newestCursor")), cursor)
            if newest:
                self.state["cursors"][channel] = newest
                self._save()  # No open transaction/network overlap; advance only after core acceptance.

    async def _run(self):
        try:
            while True:
                await asyncio.sleep(5)
                await self._reconcile()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - report safe boundary error without provider payloads or credentials
            self.error = "Beeper reconciliation stopped; reconnect to resume from the durable cursor"

    async def history(self, channel: str, limit: int = 10):
        if not 1 <= limit <= 1000:
            raise ValueError("Beeper history limit must be between 1 and 1000")
        chat = await self._chat(channel)
        if not self.identity:
            await self.check()
        result, params, visited = [], {}, set()
        while len(result) < limit:
            page = await self._request("GET", f"/v1/chats/{self._part(channel)}/messages", params=params)
            result.extend(page["items"])
            if not page["hasMore"]:
                break
            cursor = page.get("oldestCursor")
            if not cursor or cursor in visited:
                raise RuntimeError("Beeper history pagination stopped advancing")
            visited.add(cursor)
            params = {"cursor": cursor, "direction": "before"}
        return [self._message(m, chat) for m in sorted(result, key=lambda m: (m["timestamp"], m["sortKey"]))[-limit:]]

    async def _confirmed_send(self, channel, payload):
        result = await self._request("POST", f"/v1/chats/{self._part(channel)}/messages", json=payload)
        if result.get("chatID") != channel or not result.get("pendingMessageID"):
            raise RuntimeError("Beeper send routing is uncertain; inspect the chat before retrying")
        pending = result["pendingMessageID"]
        try:
            async with asyncio.timeout(60):
                while True:
                    message = await self._request(
                        "GET", f"/v1/chats/{self._part(channel)}/messages/{self._part(pending)}", allow_missing=True
                    )
                    if message is None:
                        await asyncio.sleep(1)
                        continue
                    if message.get("accountID") != self.network_account or message.get("chatID") != channel:
                        raise RuntimeError("Beeper send confirmation belongs to another account or chat")
                    status = message.get("sendStatus", {}).get("status")
                    if status == "SUCCESS":
                        return message["id"]
                    if status in {"FAIL_RETRIABLE", "FAIL_PERMANENT"}:
                        raise RuntimeError("Beeper reports failed delivery; inspect the chat before retrying")
                    await asyncio.sleep(1)
        except TimeoutError:
            raise RuntimeError("Beeper delivery is still unconfirmed; inspect the chat before retrying") from None

    async def send(self, channel: str, text: str, thread: str = "", media: list[dict] | None = None):
        chat = await self._chat(channel)
        if chat.get("isReadOnly"):
            raise ValueError("This Beeper conversation is read-only")
        caps = chat.get("capabilities") or {}
        if caps.get("maxTextLength") and len(text) > caps["maxTextLength"]:
            raise ValueError("Message exceeds this Beeper network's text limit")
        if thread and caps.get("reply", 0) <= 0:
            raise ValueError("This Beeper conversation does not support replies")
        attachments = []
        for item in media or []:
            mime = item.get("mimetype", "application/octet-stream")
            kind = mime.split("/")[0] if mime.split("/")[0] in {"image", "audio", "video"} else "file"
            capability = caps.get("attachments", {}).get("m." + kind, {})
            if not any(
                level > 0 and fnmatch.fnmatchcase(mime, pattern)
                for pattern, level in capability.get("mimeTypes", {}).items()
            ):
                raise ValueError("This Beeper conversation does not support the attachment MIME type")
            data = base64.b64decode(item["data"], validate=True)
            if not data or (capability.get("maxSize") and len(data) > capability["maxSize"]):
                raise ValueError("Beeper attachment is empty or exceeds this network's limit")
            attachments.append((item, kind, capability))
        if not text and not attachments:
            raise ValueError("A Beeper message requires text or an attachment")
        async with self.send_lock:
            payloads = []
            caption = bool(
                text
                and attachments
                and attachments[0][2].get("caption", 0) > 0
                and (
                    not attachments[0][2].get("maxCaptionLength") or len(text) <= attachments[0][2]["maxCaptionLength"]
                )
            )
            if text and not caption:
                payloads.append({"text": text})
            for index, (item, kind, _) in enumerate(attachments):
                uploaded = await self._request(
                    "POST",
                    "/v1/assets/upload/base64",
                    json={
                        "content": item["data"],
                        "fileName": Path(item.get("filename", "attachment")).name,
                        "mimeType": item.get("mimetype", "application/octet-stream"),
                    },
                )
                if not uploaded.get("uploadID") or uploaded.get("error"):
                    raise RuntimeError("Beeper did not accept the attachment upload")
                payload = {"attachment": {"uploadID": uploaded["uploadID"], "type": kind}}
                if index == 0 and caption:
                    payload["text"] = text
                payloads.append(payload)
            sent = []
            try:
                for payload in payloads:
                    if thread:
                        payload["replyToMessageID"] = thread
                    sent.append(await self._confirmed_send(channel, payload))
            except Exception:
                if sent:
                    raise RuntimeError("Beeper message partially delivered; inspect the chat before retrying") from None
                raise
            return sent[-1]

    async def react(self, channel: str, message_id: str, status: str):
        chat = await self._chat(channel)
        caps = chat.get("capabilities") or {}
        if caps.get("reaction", 0) <= 0:
            raise ValueError("This Beeper conversation does not support reactions")
        emoji = {"runner": "⚡", "white_check_mark": "👍", "x": "👎"}[status]
        if "allowedReactions" in caps and emoji not in caps["allowedReactions"]:
            raise ValueError("This Beeper conversation does not support the requested reaction")
        return await self._request(
            "POST",
            f"/v1/chats/{self._part(channel)}/messages/{self._part(message_id)}/reactions",
            json={"reactionKey": emoji},
        )

    async def close(self):
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
            self.task = None
        await self.client.aclose()
