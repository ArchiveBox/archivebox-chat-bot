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
        finally:
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
            if connection.platform == "irc":
                from .transports.irc import IRCTransport

                adapter = IRCTransport
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
            "team_id": self.options.get("server") or self.options.get("homeserver", ""),
        }
        self.capabilities = result.get("capabilities", {})
        return result

    async def receive(self, raw):
        await self.ready.wait()
        message = Message(
            platform=self.settings.platform,
            role=self.role,
            channel=str(raw["channel"]),
            user=str(raw["user"]),
            user_name=raw.get("user_name", str(raw["user"])),
            text=raw.get("text", ""),
            ts=str(raw["id"]),
            thread=raw.get("thread", ""),
            is_dm=raw.get("is_dm", False),
            is_mention=raw.get("is_mention", False),
            is_bot=raw.get("is_bot", False),
        )
        self.users[message.user] = {"name": message.user_name, "is_bot": message.is_bot, "is_guest": False}
        # Native slash command suffixes identify Telegram bots in shared groups.
        command = re.match(
            r"^/(?:archivebox(?:@\w+)?\s+)?(help|save|search|status|auto)(?:@\w+)?(?:\s+(.*))?$",
            message.text.strip(),
            re.DOTALL,
        )
        if self.role == "capture" and command:
            message.command, message.text = command[1], command[2] or ""
        await self.on_message(message)

    async def user(self, user_id):
        if user_id in self.users:
            return self.users[user_id]
        row = self.store.db.execute(
            "SELECT user_name,is_bot FROM history WHERE connection=? AND user_id=? ORDER BY seq DESC LIMIT 1",
            (self.settings.id, user_id),
        ).fetchone()
        return {"name": row[0] if row else user_id, "is_bot": bool(row[1]) if row else False, "is_guest": False}

    async def recent(self, message):
        return self.store.recent(self.settings.id, message)

    async def react(self, message, status):
        if self.capabilities.get("reactions"):
            await self.call("react", channel=message.channel, message_id=message.ts, status=status)

    async def post_text(self, message, text):
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
        title = " ".join((snapshot.get("title") or snapshot["url"]).split())[:100]
        text = f"✅ [{title}]({detail_url}) · {snapshot['url']} · {size_label(snapshot.get('output_size', 0))} · 👤 {snapshot.get('persona') or 'Default'}"
        files = []
        if self.capabilities.get("media"):
            for kind, artifact in media.items():
                data = normalize_image(artifact[0], kind)
                files.append(
                    {"filename": f"{kind}.png", "mimetype": "image/png", "data": base64.b64encode(data).decode()}
                )
        else:
            text = f"✅ {title} — {detail_url} · {snapshot['url']} · {size_label(snapshot.get('output_size', 0))} · 👤 {snapshot.get('persona') or 'Default'}"
        result = await self.call("send", channel=self.settings.saved_channel, text=text, media=files)
        return result.get("id", "") if isinstance(result, dict) else result

    async def channels(self):
        result = await self.call("channels")
        return result.get("channels", []) if isinstance(result, dict) else result

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
                bot = TransportBot(
                    connection, role, self.store, self.worker if connection.platform in NODE_PLATFORMS else None
                )
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
                if connection.platform in NODE_PLATFORMS:
                    self.create(connection, role, settings)
                    accounts.append(
                        {
                            "id": f"{connection.id}:{role}",
                            "platform": connection.platform,
                            "options": connection.account_options(role),
                            "data_dir": str(self.store.directory.resolve() / "accounts" / f"{connection.id}:{role}"),
                        }
                    )
        if accounts:
            await self.worker.start()
            await self.worker.call("configure", params={"accounts": accounts})

    async def close(self):
        await self.worker.close()
