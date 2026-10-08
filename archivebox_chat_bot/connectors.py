"""Chat SDK subprocess and a common surface for maintained platform transports."""

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import uuid
from pathlib import Path

from .media import normalize_image
from .models import Message
from .slack import Slack
from .text import size_label
from .zulip import Zulip

log = logging.getLogger(__name__)
NODE_PLATFORMS = {"telegram", "whatsapp", "messenger"}


def uses_sdk(connection, role):
    return connection.platform in NODE_PLATFORMS and not (
        connection.platform == "whatsapp" and connection.account_options(role).get("transport") == "agent"
    )


class ConnectorWorker:
    def __init__(self):
        self.process = None
        self.pending = {}
        self.receivers = {}
        self.status = {}
        self.deliveries = set()
        self.reader = None

    async def start(self):
        script = Path(os.environ.get("CONNECTOR_WORKER", Path(__file__).parent.parent / "connectors/dist/worker.js"))
        if not script.exists():
            raise RuntimeError(
                "Build the connectors with npm ci && npm run build in connectors/, or use the Docker image"
            )
        self.process = await asyncio.create_subprocess_exec(
            os.environ.get("NODE", "node"),
            str(script),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=16 * 1024 * 1024,
        )
        self.reader = asyncio.create_task(self.read())

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                data = json.loads(line)
                if "id" in data:
                    future = self.pending.pop(data["id"], None)
                    if future and not future.done():
                        if "error" in data:
                            future.set_exception(RuntimeError(data["error"]))
                        else:
                            future.set_result(data.get("result"))
                elif data.get("event") == "status":
                    self.status[data["account"]] = data
                elif data.get("event") == "message":
                    task = asyncio.create_task(self.deliver(data))
                    self.deliveries.add(task)
                    task.add_done_callback(self.deliveries.discard)
        except Exception:
            log.exception("Chat connector event parsing failed")
            raise
        finally:
            for account in self.receivers:
                self.status[account] = {"state": "error", "detail": "Chat connector stopped; reconnect this account"}
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Chat connector stopped; inspect connection status"))
            self.pending.clear()

    async def deliver(self, data):
        try:
            await self.receivers[data["account"]](data["message"])
            await self.call("ack", data["account"], {"delivery_id": data["delivery_id"]})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Connector message was not acknowledged; retained in durable ingress")

    async def call(self, method, account=None, params=None):
        if not self.process or self.process.returncode is not None:
            raise RuntimeError("Chat connector is not running")
        key = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            self.process.stdin.write(
                (json.dumps({"id": key, "method": method, "account": account, "params": params or {}}) + "\n").encode()
            )
            await self.process.stdin.drain()
            return await asyncio.wait_for(future, timeout=90)
        finally:
            self.pending.pop(key, None)

    async def close(self):
        if self.process:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.call("close"), timeout=10)
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=10)
            except TimeoutError:
                self.process.terminate()
                await self.process.wait()
        for task in self.deliveries:
            task.cancel()
        await asyncio.gather(*self.deliveries, return_exceptions=True)
        if self.reader:
            await asyncio.gather(self.reader, return_exceptions=True)


class TransportBot:
    def __init__(self, connection, role, store, worker=None):
        self.settings, self.role, self.store, self.worker = connection, role, store, worker
        self.account = f"{connection.id}:{role}"
        self.options = connection.account_options(role)
        self.identity = {}
        self.capabilities = {}
        self.ready = asyncio.Event()
        self.on_message = None
        self.transport = None
        self.users = {}
        directory = store.directory.resolve() / "accounts" / self.account
        if worker:
            worker.receivers[self.account] = self.receive
        else:
            if connection.platform == "beeper":
                from .transports.beeper import BeeperTransport

                adapter = BeeperTransport
            elif connection.platform == "discord":
                from .transports.discord import DiscordTransport

                adapter = DiscordTransport
            elif connection.platform == "email":
                from .transports.email import EmailTransport

                adapter = EmailTransport
            elif connection.platform == "irc":
                from .transports.irc import IRCTransport

                adapter = IRCTransport
            elif connection.platform == "whatsapp":
                from .transports.whatsapp_agent import WhatsAppAgentTransport

                adapter = WhatsAppAgentTransport
            else:
                from .transports.imessage import IMessageTransport

                adapter = IMessageTransport
            self.transport = adapter(self.options, self.account, directory, self.receive)

    async def call(self, method, **params):
        if self.worker:
            return await self.worker.call(method, self.account, params)
        return await getattr(self.transport, method)(**params)

    async def start(self, on_message):
        self.on_message = on_message
        if self.transport:
            # Transports await ingestion while starting; permit it before watch/join.
            self.ready.set()
            await self.transport.start()
        result = await self.check()
        if result.get("state") == "error":
            raise RuntimeError(result.get("detail") or "Connector could not start")
        self.ready.set()

    async def check(self):
        result = await self.call("check")
        self.identity = {
            "user_id": str(result.get("bot_user_id") or result.get("id") or ""),
            "team_id": result.get("guild_id")
            or self.options.get("server")
            or self.options.get("homeserver")
            or self.options.get("base_url", ""),
        }
        self.capabilities = result.get("capabilities", {})
        if result.get("state", "connected") != "connected":
            self.identity = {}
        self.username = result.get("username") or result.get("name") or self.options.get("username", "")
        return result

    async def receive(self, raw):
        await self.ready.wait()
        if self.transport or not self.identity.get("user_id"):
            await self.check()
        if not self.identity.get("user_id"):
            raise RuntimeError("The connected bot identity is not available yet")
        message = Message(
            platform=self.settings.platform,
            role=self.role,
            channel=str(raw["channel"]),
            user=str(raw["user"]),
            user_name=raw.get("user_name", str(raw["user"])),
            channel_name=raw.get("channel_name", ""),
            source_platform=raw.get("source_platform", ""),
            text=raw.get("text", ""),
            ts=str(raw["id"]),
            thread=raw.get("thread", ""),
            is_dm=raw.get("is_dm", False),
            is_mention=raw.get("is_mention", False),
            is_bot=raw.get("is_bot", False),
            command=raw.get("command", ""),
            native_command=raw.get("native_command", False),
        )
        self.users[message.user] = {"name": message.user_name, "is_bot": message.is_bot, "is_guest": False}
        if self.settings.platform == "telegram" and message.text.startswith("/"):
            addressed = re.match(r"^/\w+@([\w]+)(?:\s|$)", message.text)
            if addressed and addressed[1].lower() != self.username.lstrip("@").lower():
                return
        # Native slash command suffixes identify Telegram bots in shared groups.
        command = re.match(
            r"^/(?:archivebox(?:@\w+)?\s+)?(help|save|search|status|auto)(?:@\w+)?(?:\s+(.*))?$",
            message.text.strip(),
            re.DOTALL,
        )
        # Explicit save commands on the AI bot opt into the regular capture
        # path. Other AI text remains an OpenCode request.
        if command and (self.role == "capture" or (self.role == "ai" and command[1] == "save")):
            message.command, message.text = command[1], command[2] or ""
        return await self.on_message(message)

    async def user(self, user_id):
        if user_id in self.users:
            return self.users[user_id]
        row = self.store.db.execute(
            "SELECT user_name,is_bot FROM history WHERE connection=? AND role=? AND user_id=? ORDER BY seq DESC LIMIT 1",
            (self.settings.id, self.role, user_id),
        ).fetchone()
        return {"name": row[0] if row else user_id, "is_bot": bool(row[1]) if row else False, "is_guest": False}

    async def recent(self, message):
        if self.capabilities.get("history") is True:
            return await self.call("recent", channel=message.channel, message_id=message.ts, thread=message.thread)
        return self.store.recent(self.settings.id, message)

    async def react(self, message, status):
        if message.native_command:
            return
        if self.capabilities.get("reactions"):
            await self.call("react", channel=message.channel, message_id=message.ts, status=status)

    async def post_text(self, message, text):
        if message.native_command:
            return await self.call("respond", message_id=message.ts, text=text)
        result = await self.call(
            "send",
            channel=message.channel,
            thread=message.thread if self.capabilities.get("threads", True) else "",
            text=text,
        )
        message_id = result.get("id", "") if isinstance(result, dict) else str(result or "")
        if message_id:
            self.store.remember(
                self.settings.id,
                Message(
                    self.settings.platform,
                    self.role,
                    message.channel,
                    self.identity.get("user_id", self.account),
                    text,
                    message_id,
                    thread=message.thread,
                    is_dm=message.is_dm,
                    is_bot=True,
                ),
            )
        return message_id

    async def post_card(self, snapshot, detail_url, media, client_id):
        if self.capabilities.get("cards"):
            return await self.call(
                "post_card", channel=self.settings.saved_channel, snapshot=snapshot, detail_url=detail_url, media=media
            )
        title = " ".join((snapshot.get("title") or snapshot["url"]).split())[:100]
        markdown = self.settings.platform == "telegram" or self.options.get("transport") == "matrix"
        headline = f"[{title}]({detail_url})" if markdown else f"{title} — {detail_url}"
        text = f"✅ {headline} · {snapshot['url']} · Saved {size_label(snapshot.get('output_size', 0))} · 👤 {snapshot.get('persona') or 'Default'}"
        if favicon := media.get("favicon"):
            text += f" · [🌐]({favicon.url})" if markdown else f" · 🌐 {favicon.url}"
        files = []
        if self.capabilities.get("media") and (screenshot := media.get("screenshot")):
            files.append(
                {
                    "filename": "screenshot.png",
                    "mimetype": "image/png",
                    "data": base64.b64encode(normalize_image(screenshot.data, "screenshot")).decode(),
                }
            )
        result = await self.call("send", channel=self.settings.saved_channel, text=text, media=files)
        return result.get("id", "") if isinstance(result, dict) else result

    async def channels(self):
        result = await self.call("channels")
        return result.get("channels", []) if isinstance(result, dict) else result

    async def setup_channels(self):
        if self.transport and hasattr(self.transport, "setup_channels"):
            return await self.transport.setup_channels(self.settings)
        return {}

    async def close(self):
        if self.transport:
            await self.transport.close()


class Adapters:
    def __init__(self, store):
        self.store = store
        self.worker = ConnectorWorker()
        self.bots = {}

    def create(self, connection, role, settings):
        key = f"{connection.id}:{role}"
        if key not in self.bots:
            if connection.platform == "slack":
                bot = Slack(connection, role, archive_url=settings.archivebox_public_url)
            elif connection.platform == "zulip":
                bot = Zulip(connection, role, store=self.store)
            else:
                bot = TransportBot(connection, role, self.store, self.worker if uses_sdk(connection, role) else None)
            self.bots[key] = bot
        return self.bots[key]

    async def configure(self, settings):
        accounts = []
        for connection in settings.connections:
            if not connection.enabled:
                continue
            for role in ("capture", "ai"):
                if not getattr(connection, role).enabled:
                    continue
                if uses_sdk(connection, role):
                    self.create(connection, role, settings)
                    accounts.append(
                        {
                            "id": f"{connection.id}:{role}",
                            "platform": connection.platform,
                            "options": connection.account_options(role),
                            "data_dir": str(self.store.directory.resolve() / "accounts" / f"{connection.id}:{role}"),
                        }
                    )
        if accounts and (not self.worker.process or self.worker.process.returncode is not None):
            await self.worker.start()
        if self.worker.process:
            await self.worker.call("configure", params={"accounts": accounts})

    async def close(self):
        await self.worker.close()
