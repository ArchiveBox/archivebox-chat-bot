"""Native Zulip REST API adapter (https://zulip.com/api/)."""

import asyncio
import contextlib
import json
import logging
import re
from collections.abc import Awaitable, Callable
from urllib.parse import quote, urlsplit

import httpx

from .config import Connection
from .media import normalize_image
from .models import Message
from .text import size_label

logger = logging.getLogger(__name__)


class ZulipError(RuntimeError):
    def __init__(self, result: dict):
        self.code = result.get("code", "")
        super().__init__(f"Zulip: {result.get('msg') or self.code or 'request failed'}")


def _markdown(value: str) -> str:
    # Titles/personas are display text, and must not create mentions or Markdown links.
    return re.sub(r"([\\`*_\[\]<>@])", r"\\\1", str(value)).replace("\n", " ").replace("\r", " ")


def _link(value: str) -> str:
    return quote(str(value), safe=":/?#&=;%+~!$,-._")


class Zulip:
    def __init__(self, settings: Connection, role: str = "capture", store=None):
        if role not in ("capture", "ai"):
            raise ValueError("Choose a capture or ai Zulip bot")
        self.settings, self.role = settings, role
        self.options = settings.account_options(role)
        self.capabilities = {"reactions": True, "media": True, "history": True}
        self.identity = {}
        self.store = store
        self.message_cursor = 0
        self.cursor_initialized = False
        self.cursor_key = ""
        email = self.options.get("email", "")
        key = self.options.get("api_key", "")
        if not self.options.get("url", "") or not email or not key:
            raise ValueError(f"Enter the Zulip server URL and {role} bot email/API key")
        self.client = httpx.AsyncClient(
            base_url=f"{self.options['url']}/api/v1/",
            auth=(email, key),
            timeout=30,
            headers={"User-Agent": "ArchiveBox-Slack/0.1 (Zulip)"},
        )
        self.bot_id = ""
        self.bot_name = ""
        self.queue_id = ""
        self.last_event_id = -1
        self.poll_timeout = 90.0
        self.task: asyncio.Task | None = None

    async def _api(self, method: str, path: str, data: dict | None = None, **kwargs) -> dict:
        encoded = {
            key: json.dumps(value) if isinstance(value, (list, dict, bool)) else str(value)
            for key, value in (data or {}).items()
        }
        kwargs["params" if method == "GET" else "data"] = encoded
        response = await self.client.request(method, path, **kwargs)
        if "application/json" not in response.headers.get("content-type", ""):
            response.raise_for_status()
            raise RuntimeError("Zulip returned a non-JSON response; check the server URL")
        result = response.json()
        if result.get("result") != "success":
            raise ZulipError(result)
        response.raise_for_status()
        return result

    async def check(self) -> dict:
        result = await self._api("GET", "users/me")
        if not result["is_bot"]:
            raise ValueError("Use a generic Zulip bot's email/API key, rather than a personal account")
        self.bot_id, self.bot_name = str(result["user_id"]), result["full_name"]
        return {"bot_id": self.bot_id, "bot_name": self.bot_name}

    async def channels(self):
        result = await self._api("GET", "users/me/subscriptions")
        return [
            {"id": str(item["stream_id"]), "name": item["name"], "is_dm": False} for item in result["subscriptions"]
        ]

    async def setup_channels(self) -> dict:
        channels = {}
        for field, enabled in (
            ("new_channel", self.settings.enable_new_urls),
            ("saved_channel", self.settings.enable_saved_urls),
        ):
            if not enabled:
                channels[field] = getattr(self.settings, field)
                continue
            channel_id = getattr(self.settings, field)
            if channel_id:
                stream = (await self._api("GET", f"streams/{int(channel_id)}"))["stream"]
                name = stream["name"]
            else:
                name = getattr(self.settings, f"{field}_name")
            await self._api(
                "POST",
                "users/me/subscriptions",
                {
                    "subscriptions": [{"name": name}],
                    "invite_only": True,
                },
            )
            result = await self._api("GET", "get_stream_id", {"stream": name})
            channels[field] = str(result["stream_id"])
            setattr(self.settings, field, channels[field])
        return channels

    async def user(self, user_id: str) -> dict:
        result = (await self._api("GET", f"users/{int(user_id)}"))["user"]
        return {"name": result["full_name"], "is_bot": result["is_bot"], "is_guest": result["is_guest"]}

    async def _register(self) -> None:
        result = await self._api(
            "POST",
            "register",
            {
                "event_types": ["message"],
                "fetch_event_types": ["realm"],
                "apply_markdown": False,
                "client_capabilities": {"notification_settings_null": True, "empty_topic_name": True},
            },
        )
        self.queue_id = result["queue_id"]
        self.last_event_id = result["last_event_id"]
        self.poll_timeout = float(result["event_queue_longpoll_timeout_seconds"])

    async def start(self, on_message: Callable[[Message], Awaitable[None]]) -> None:
        if self.task and not self.task.done():
            return
        await self.check()
        self.cursor_key = f"zulip:{self.options['url']}:{self.bot_id}:message_cursor"
        if self.store:
            saved = self.store.meta(self.cursor_key)
            self.cursor_initialized = saved != ""
            self.message_cursor = int(saved) if saved else 0
        await self._register()
        await self._recover(on_message)
        self.task = asyncio.create_task(self._poll(on_message), name=f"zulip-{self.role}")

    def _save_cursor(self, message_id: int) -> None:
        self.message_cursor = max(self.message_cursor, message_id)
        self.cursor_initialized = True
        if self.store:
            self.store.set_meta(self.cursor_key, self.message_cursor)

    async def _dispatch(self, event: dict, on_message: Callable[[Message], Awaitable[None]]) -> None:
        message = await self._message(event)
        if message:
            await on_message(message)
        # The parent callback durably enqueues before returning. Never acknowledge before that succeeds.
        self._save_cursor(event["message"]["id"])

    async def _recover(self, on_message: Callable[[Message], Awaitable[None]]) -> None:
        if not self.cursor_initialized:
            latest = await self._api(
                "GET",
                "messages",
                {
                    "anchor": "newest",
                    "num_before": 1,
                    "num_after": 0,
                    "apply_markdown": False,
                },
            )
            self._save_cursor(latest["messages"][-1]["id"] if latest["messages"] else 0)
            return
        while True:
            # No shared-history narrow: only the bot's subscribed channels and direct messages are read.
            page = await self._api(
                "GET",
                "messages",
                {
                    "anchor": self.message_cursor,
                    "num_before": 0,
                    "num_after": 100,
                    "include_anchor": False,
                    "apply_markdown": False,
                    "allow_empty_topic_name": True,
                },
            )
            for raw in page["messages"]:
                await self._dispatch({"message": raw, "flags": raw["flags"]}, on_message)
            if not page["messages"] or page["found_newest"]:
                return

    async def _poll(self, on_message: Callable[[Message], Awaitable[None]]) -> None:
        try:
            while True:
                try:
                    result = await self._api(
                        "GET",
                        "events",
                        {"queue_id": self.queue_id, "last_event_id": self.last_event_id},
                        timeout=self.poll_timeout,
                    )
                except ZulipError as exc:
                    if exc.code != "BAD_EVENT_QUEUE_ID":
                        raise
                    logger.warning("Zulip event queue expired; registering a new queue")
                    await self._register()
                    await self._recover(on_message)
                    continue
                except httpx.TransportError:
                    # Keep the same cursor and queue when the network drops, so unacknowledged events replay.
                    logger.warning("Zulip event connection interrupted; reconnecting", exc_info=True)
                    await asyncio.sleep(5)
                    continue
                for event in result["events"]:
                    if event["type"] == "message":
                        await self._dispatch(event, on_message)
                    self.last_event_id = event["id"]
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Zulip event listener stopped")
            raise

    async def _message(self, event: dict) -> Message | None:
        raw = event["message"]
        sender = str(raw["sender_id"])
        if sender == self.bot_id:
            return None
        is_dm = raw["type"] == "private"
        is_mention = "mentioned" in event["flags"]
        channel = (
            ",".join(
                str(uid) for uid in sorted({user["id"] for user in raw["display_recipient"]}) if str(uid) != self.bot_id
            )
            if is_dm
            else str(raw["stream_id"])
        )
        # Addressing the bot plus a known keyword provides Zulip's native command surface.
        # Keep disabled commands recognizable so Engine applies the configured command allowlist.
        text = re.sub(
            rf"@\*\*(?:[^*|]+\|{re.escape(self.bot_id)}|{re.escape(self.bot_name)})\*\*", "", raw["content"]
        ).strip()
        command = ""
        if self.role == "capture" and (is_dm or is_mention):
            pieces = text.split(maxsplit=1)
            if pieces and pieces[0].casefold() in {"help", "save", "search", "status", "auto"}:
                command = pieces[0].casefold()
                text = pieces[1] if len(pieces) > 1 else ""
        if (await self.user(sender))["is_bot"]:
            return None
        return Message(
            platform="zulip",
            role=self.role,
            channel=channel,
            user=sender,
            text=text.strip(),
            ts=str(raw["id"]),
            thread="" if is_dm else raw["subject"],
            is_dm=is_dm,
            is_mention=is_mention,
            command=command,
            user_name=raw.get("sender_full_name", sender),
            channel_name="" if is_dm else raw["display_recipient"],
        )

    async def recent(self, message: Message) -> list[str]:
        narrow = (
            [{"operator": "dm", "operand": [int(uid) for uid in message.channel.split(",")]}]
            if message.is_dm
            else [
                {"operator": "stream", "operand": int(message.channel)},
                {"operator": "topic", "operand": message.thread},
            ]
        )
        anchor = int(message.ts)
        humans, users = [], {}
        while len(humans) < 10:
            result = await self._api(
                "GET",
                "messages",
                {
                    "anchor": anchor,
                    "num_before": 100,
                    "num_after": 0,
                    "include_anchor": False,
                    "narrow": narrow,
                    "apply_markdown": False,
                    "allow_empty_topic_name": True,
                },
            )
            rows = result["messages"]
            for row in reversed(rows):
                if row["id"] >= int(message.ts):
                    continue
                sender = str(row["sender_id"])
                if sender not in users:
                    users[sender] = await self.user(sender)
                if not users[sender]["is_bot"] or (self.role == "ai" and sender == self.bot_id):
                    humans.append(f"{users[sender]['name']}: {row['content']}")
                    if len(humans) == 10:
                        break
            if not rows or result["found_oldest"]:
                break
            anchor = rows[0]["id"]
        return list(reversed(humans))

    async def react(self, message: Message, status: str) -> None:
        name, code = {
            "runner": ("running", "1f3c3"),
            "white_check_mark": ("check", "2705"),
            "x": ("cross_mark", "274c"),
        }[status]
        path = f"messages/{int(message.ts)}/reactions"
        try:
            await self._api("POST", path, {"emoji_name": name, "emoji_code": code, "reaction_type": "unicode_emoji"})
        except ZulipError as exc:
            if exc.code != "REACTION_ALREADY_EXISTS":
                raise
        if status != "runner":
            try:
                await self._api(
                    "DELETE",
                    path,
                    {
                        "emoji_name": "running",
                        "emoji_code": "1f3c3",
                        "reaction_type": "unicode_emoji",
                    },
                )
            except ZulipError as exc:
                if exc.code != "REACTION_DOES_NOT_EXIST":
                    raise

    async def post_text(self, message: Message, text: str) -> str:
        data = {"type": "direct" if message.is_dm else "stream", "content": text}
        data["to"] = [int(uid) for uid in message.channel.split(",")] if message.is_dm else int(message.channel)
        if not message.is_dm:
            data["topic"] = message.thread
        return str((await self._api("POST", "messages", data))["id"])

    async def post_card(
        self, snapshot: dict, detail_url: str, media: dict[str, tuple[bytes, str]], delivery_id: str
    ) -> str:
        if not self.settings.saved_channel:
            raise ValueError("Choose a saved URLs channel before publishing archive cards")
        content = [
            f"**[{_markdown(snapshot.get('title') or snapshot['url'])}]({_link(detail_url)})**",
            f"[{_markdown(urlsplit(snapshot['url']).hostname or snapshot['url'])}]({_link(snapshot['url'])})",
            (
                f"Saved {size_label(int(snapshot.get('output_size') or 0))} · "
                f"👤 {_markdown(snapshot.get('persona') or 'Default')}"
            ),
        ]
        if self.settings.upload_images:
            for kind, artifact in media.items():
                result = await self._api(
                    "POST",
                    "user_uploads",
                    files={
                        "filename": (f"{kind}.png", normalize_image(artifact.data, kind), "image/png"),
                    },
                )
                label = {"screenshot": "📷", "favicon": "🌐"}[kind]
                content.append(f"[{label}]({_link(result['url'])})")
        result = await self._api(
            "POST",
            "messages",
            {
                "type": "stream",
                "to": int(self.settings.saved_channel),
                "topic": "Saved URLs",
                "content": " · ".join(content),
            },
        )
        return str(result["id"])

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.task
            self.task = None
        try:
            if self.queue_id:
                try:
                    await self._api("DELETE", "events", {"queue_id": self.queue_id})
                except ZulipError as exc:
                    if exc.code != "BAD_EVENT_QUEUE_ID":
                        raise
                self.queue_id = ""
        finally:
            await self.client.aclose()
