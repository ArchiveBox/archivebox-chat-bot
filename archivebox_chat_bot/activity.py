"""Persist application warnings and failures without tracebacks or credentials."""

import asyncio
import logging

from .engine import safe_error


class ActivityHandler(logging.Handler):
    def __init__(self, store):
        super().__init__(logging.WARNING)
        self.store = store
        self.loop = asyncio.get_running_loop()

    def emit(self, record):
        message = record.getMessage()
        if record.exc_info and record.exc_info[1]:
            error = record.exc_info[1]
            message += f": {type(error).__name__}: {safe_error(error)}"
        # Some provider SDKs log from worker threads. Keep SQLite on its owner loop.
        self.loop.call_soon_threadsafe(self.write, record.name, record.levelname.lower(), message)

    def write(self, source, level, message):
        self.store.event("runtime", f"{source}: {message}", level=level)
