"""One service, one ArchiveBox connection, any number of chat connections."""

import asyncio
import contextlib

from .archivebox import ArchiveBox
from .connectors import Adapters
from .engine import Engine, safe_error


class Runtime:
    def __init__(self, store):
        self.store = store
        self.engines = {}
        self.adapters = Adapters(store)
        self.connections = {}
        self.error = ""
        self.lock = asyncio.Lock()

    async def start(self):
        settings = self.store.settings()
        self.settings = settings
        self.archive = ArchiveBox(settings)
        if settings.archivebox_token:
            try:
                self.connections["archivebox"] = await asyncio.wait_for(self.archive.check(), timeout=8)
            except Exception as exc:
                self.connections["archivebox"] = {"ok": False, "error": safe_error(exc)}
        self.adapters = Adapters(self.store)
        try:
            await self.adapters.configure(settings)
        except Exception as exc:
            self.error = safe_error(exc)
        for connection in settings.connections:
            if connection.enabled:
                engine = Engine(self.store, settings, connection, self.adapters)
                self.engines[connection.id] = engine
                await engine.start()

    @property
    def scopes(self):
        return {engine.scope(role) for engine in self.engines.values() for role in ("capture", "ai")}

    def status(self):
        connections = {}
        for key, engine in self.engines.items():
            statuses = {}
            for role, bot in engine.bots.items():
                status = dict(engine.connections.get(role, {}))
                status.update(self.adapters.worker.status.get(f"{key}:{role}", {}))
                task = getattr(bot, "task", None) or getattr(getattr(bot, "transport", None), "consumer", None)
                if task and task.done() and not task.cancelled() and task.exception():
                    status.update(ok=False, error=safe_error(task.exception()))
                if "state" in status:
                    status["ok"] = status["state"] == "connected"
                statuses[role] = status
            connections[key] = {"roles": statuses, "error": engine.error}
        return {**self.connections, "chat": connections}

    async def close(self):
        for engine in self.engines.values():
            with contextlib.suppress(Exception):
                await engine.close()
        self.engines.clear()
        await self.adapters.close()
        await self.archive.close()
        self.connections.clear()
        self.error = ""

    async def restart(self):
        async with self.lock:
            await self.close()
            await self.start()
