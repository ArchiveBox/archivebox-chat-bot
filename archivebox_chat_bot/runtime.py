"""One service, one ArchiveBox connection, any number of chat connections."""

import asyncio
import contextlib
import logging
import time

from .archivebox import ArchiveBox
from .connectors import Adapters
from .engine import Engine, safe_error

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, store):
        self.store = store
        self.settings = store.settings()
        self.archive = ArchiveBox(self.settings)
        self.engines = {}
        self.adapters = Adapters(store)
        self.connections = {}
        self.error = ""
        self.lock = asyncio.Lock()
        self.monitor_task = None
        self.last_status = {}

    async def check_archive(self):
        previous = self.connections.get("archivebox", {})
        try:
            result = await asyncio.wait_for(self.archive.check(), timeout=8)
        except Exception as exc:
            log.exception("ArchiveBox connection check failed")
            result = {"ok": False, "error": safe_error(exc), "checked_at": time.time()}
        self.connections["archivebox"] = result
        if (previous.get("ok"), previous.get("error")) != (result.get("ok"), result.get("error")):
            self.store.event(
                "connection",
                "ArchiveBox connected" if result["ok"] else f"ArchiveBox: {result['error']}",
                level="info" if result["ok"] else "error",
                elapsed_ms=result.get("latency_ms"),
            )

    async def monitor(self):
        checked = time.monotonic()
        while True:
            await asyncio.sleep(5)
            if self.settings.archivebox_token and time.monotonic() - checked >= 30:
                await self.check_archive()
                checked = time.monotonic()
            for connection, status in self.status()["chat"].items():
                for role, state in status["roles"].items():
                    identity = (connection, role)
                    value = (state.get("ok"), state.get("state"), state.get("error"), state.get("detail"))
                    if self.last_status.get(identity) != value:
                        self.last_status[identity] = value
                        detail = (
                            state.get("error")
                            or state.get("detail")
                            or state.get("state")
                            or ("Connected" if state.get("ok") else "Disconnected")
                        )
                        self.store.event(
                            "connection",
                            detail,
                            connection=connection,
                            role=role,
                            level="info" if state.get("ok") else "warning",
                        )

    async def initialize(self):
        async with self.lock:
            await self.start()

    async def start(self):
        settings = self.store.settings()
        self.settings = settings
        self.archive = ArchiveBox(settings)
        if settings.archivebox_token:
            await self.check_archive()
        self.adapters = Adapters(self.store)
        try:
            await self.adapters.configure(settings)
        except Exception as exc:
            log.exception("Connector initialization failed")
            self.error = safe_error(exc)
        for connection in settings.connections:
            if connection.enabled:
                engine = Engine(self.store, settings, connection, self.adapters)
                self.engines[connection.id] = engine
                await engine.start()
        self.monitor_task = asyncio.create_task(self.monitor())

    @property
    def scopes(self):
        return {engine.scope(role) for engine in self.engines.values() for role in ("capture", "ai")}

    def status(self):
        connections = {}
        for key, engine in self.engines.items():
            statuses = {role: dict(status) for role, status in engine.connections.items()}
            for role, bot in engine.bots.items():
                status = dict(engine.connections.get(role, {}))
                status.update(self.adapters.worker.status.get(f"{key}:{role}", {}))
                transport = getattr(bot, "transport", None)
                task = (
                    getattr(bot, "task", None)
                    or getattr(transport, "consumer", None)
                    or getattr(transport, "task", None)
                )
                if getattr(transport, "error", ""):
                    status.update(ok=False, error=transport.error)
                if task and task.done() and not task.cancelled() and task.exception():
                    status.update(ok=False, error=safe_error(task.exception()))
                if "state" in status:
                    status["ok"] = status["state"] == "connected"
                identity = getattr(bot, "identity", {})
                options = engine.connection.account_options(role)
                status["username"] = (
                    getattr(bot, "username", "")
                    or identity.get("user")
                    or options.get("username")
                    or options.get("nickname")
                    or options.get("email")
                    or identity.get("user_id")
                    or ""
                )
                statuses[role] = status
            connections[key] = {"roles": statuses, "error": engine.error}
        return {**self.connections, "chat": connections}

    async def close(self):
        if self.monitor_task:
            self.monitor_task.cancel()
            await asyncio.gather(self.monitor_task, return_exceptions=True)
            self.monitor_task = None
        for engine in self.engines.values():
            with contextlib.suppress(Exception):
                await engine.close()
        self.engines.clear()
        await self.adapters.close()
        await self.archive.close()
        self.connections.clear()
        self.error = ""
        self.last_status.clear()

    async def restart(self):
        async with self.lock:
            settings = self.store.settings()
            if settings.model_dump(exclude={"connections"}) != self.settings.model_dump(exclude={"connections"}):
                await self.close()
                await self.start()
                return
            previous = {c.id: c for c in self.settings.connections}
            desired = {c.id: c for c in settings.connections if c.enabled}
            changed = {key for key in previous.keys() | desired.keys() if previous.get(key) != desired.get(key)}
            for key in changed:
                old = self.engines.pop(key, None)
                if old:
                    await old.close()
                for role in ("capture", "ai"):
                    self.adapters.bots.pop(f"{key}:{role}", None)
            await self.adapters.configure(settings)
            self.settings = settings
            for key in changed & desired.keys():
                active = Engine(self.store, settings, desired[key], self.adapters)
                self.engines[key] = active
                await active.start()
