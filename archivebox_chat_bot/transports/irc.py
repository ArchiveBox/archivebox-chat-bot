"""IRC transport backed by pydle's TLS, SASL, IRCv3 and reconnect support."""

import asyncio
import hashlib
import re
import uuid
from pathlib import Path

import pydle


class _Client(pydle.Client):
    def __init__(self, owner, *args, **kwargs):
        self.owner = owner
        self.authenticated = False
        self.welcome = asyncio.Event()
        self.cap_lines = []
        self.delivery_lock = asyncio.Lock()
        super().__init__(*args, **kwargs)

    async def on_raw_cap_ls(self, params):
        # pydle 1.x treats the IRCv3 continuation marker as the capability list.
        self.cap_lines.append(params[-1])
        if params[0] == "*":
            return
        capabilities = " ".join(self.cap_lines)
        self.cap_lines.clear()
        await super().on_raw_cap_ls([capabilities])

    async def on_raw_001(self, message):
        await super().on_raw_001(message)
        self.welcome.set()

    async def on_raw_903(self, message):
        self.authenticated = True
        await super().on_raw_903(message)

    async def on_connect(self):
        # pydle dispatches numeric callbacks concurrently; 001 updates the nick.
        await self.welcome.wait()
        await super().on_connect()
        if self.owner.options.get("sasl_username") and not self.authenticated:
            self.owner.error = "IRC SASL authentication failed; refusing unauthenticated operation"
            self.owner.ready.set()
            await self.disconnect(expected=True)
            return
        self.owner.ready.set()
        for channel in self.owner.desired_channels:
            await self.join(channel)

    async def on_disconnect(self, expected):
        self.authenticated = False
        self.welcome.clear()
        self.cap_lines.clear()
        if not self.owner.error:
            self.owner.ready.clear()
        await super().on_disconnect(expected)

    async def on_join(self, channel, user):
        await super().on_join(channel, user)
        if self.is_same_nick(user, self.nickname):
            event = self.owner.joined.get(channel)
            if event:
                event.set()

    async def on_raw_privmsg(self, message):
        nick, _ = self._parse_user(message.source)
        if self.owner.closed or self.is_same_nick(nick, self.nickname):
            return
        await super().on_raw_privmsg(message)
        if self.owner.closed:
            return
        target, body = message.params
        tags = getattr(message, "tags", {})
        account = tags.get("account")
        # Only server-authenticated accounts are reusable authorization identities.
        # An anonymous nick/host cannot impersonate an account or survive sessions.
        if account and account != "*":
            user = f"account:{self.normalize(account)}"
        else:
            digest = hashlib.sha256(f"{self.owner.session}:{message.source}".encode()).hexdigest()[:24]
            user = f"anonymous:{digest}"
        self.owner.sequence += 1
        message_id = tags.get("msgid") or f"{self.owner.session}:{self.owner.sequence}"
        is_dm = not self.is_channel(target)
        event = {
            "id": message_id,
            "channel": nick if is_dm else target,
            "user": user,
            "user_name": nick,
            "text": body,
            "thread": tags.get("+draft/reply", ""),
            "is_dm": is_dm,
            "is_mention": bool(re.search(r"(?<![\w-])" + re.escape(self.nickname) + r"(?![\w-])", body, re.IGNORECASE)),
            "is_bot": False,
        }
        async with self.delivery_lock:
            await self.owner.emit(event)


class IRCTransport:
    def __init__(self, options: dict, account_id: str, data_dir: Path, emit):
        self.options, self.account_id, self.emit = options, account_id, emit
        self.session, self.sequence = uuid.uuid4().hex, 0
        self.ready = asyncio.Event()
        self.desired_channels = set(options.get("channels", []))
        self.joined = {}
        self.error = ""
        self.closed = False
        self.client = _Client(
            self,
            options.get("nickname", "archivebox"),
            realname="ArchiveBox",
            sasl_username=options.get("sasl_username"),
            sasl_password=options.get("sasl_password"),
        )

    async def start(self):
        try:
            await self.client.connect(
                self.options["server"],
                int(self.options.get("port", 6697 if self.options.get("tls", True) else 6667)),
                tls=self.options.get("tls", True),
                tls_verify=True,
                password=self.options.get("server_password"),
            )
            await asyncio.wait_for(self.ready.wait(), 30)
            if self.error:
                raise RuntimeError(self.error)
        except Exception:  # noqa: BLE001 - transport boundary must redact credentials and surface failure
            await self.close()
            raise RuntimeError("IRC connection failed; check server, TLS certificate and SASL credentials") from None

    async def check(self):
        if not self.client.connected or not self.client.registered or self.error:
            raise RuntimeError(self.error or "IRC is disconnected")
        return {
            "id": ("account:" + self.client.normalize(self.options["sasl_username"]))
            if self.options.get("sasl_username")
            else self.client.nickname,
            "name": self.client.nickname,
            "capabilities": {
                "reactions": False,
                "media": False,
                "threads": False,
                "authenticated_users": bool(self.client._capabilities.get("account-tag")),
            },
        }

    async def send(self, channel: str, text: str, thread: str = "", media: list[dict] | None = None):
        if media:
            raise ValueError("IRC does not support media uploads; send a public URL as text")
        if thread:
            raise ValueError("IRC native thread replies are unsupported")
        if not channel or any(char in channel for char in "\r\n\0 ,"):
            raise ValueError("Invalid IRC destination")
        await self.check()
        await self.client.message(channel, text)
        # IRC PRIVMSG has no delivery receipt; this is a local submission ID.
        return f"submitted:{uuid.uuid4().hex}"

    async def react(self, channel: str, message_id: str, status: str):
        raise NotImplementedError("IRC reactions are not supported by this transport")

    async def join(self, channel: str):
        if not self.client.is_channel(channel) or any(char in channel for char in "\r\n\0 ,"):
            raise ValueError("Invalid IRC channel")
        if channel not in self.client.channels:
            event = self.joined.setdefault(channel, asyncio.Event())
            await self.client.join(channel)
            try:
                await asyncio.wait_for(event.wait(), 15)
            except TimeoutError:
                raise RuntimeError(
                    "IRC channel join was not confirmed; check invitation and channel permissions"
                ) from None
            finally:
                self.joined.pop(channel, None)
        self.desired_channels.add(channel)
        return {"id": channel, "name": channel}

    async def channels(self):
        return [{"id": channel, "name": channel} for channel in self.client.channels]

    async def close(self):
        self.closed = True
        self.client.RECONNECT_ON_ERROR = False
        await self.client.disconnect(expected=True)
