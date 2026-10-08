"""Real Slack channel setup through the console API.

SLACK_TEST_DATA_DIR=/path/to/configured/data uv run pytest -xq tests/test_slack_setup_live.py
Creates two dedicated channels, leaving them available to inspect in Slack.
"""

import os
import sqlite3
import uuid
from pathlib import Path

import aiohttp
from slack_sdk.web.async_client import AsyncWebClient

from archivebox_chat_bot.config import Settings


async def test_slack_console_provisions_and_reuses_destinations(console, tmp_path):
    source = Path(os.environ["SLACK_TEST_DATA_DIR"]) / "bridge.sqlite3"
    with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as db:
        settings = Settings.model_validate_json(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
    original = next(c for c in settings.connections if c.platform == "slack" and c.capture.enabled)
    token = original.account_options("capture")["bot_token"]
    suffix = uuid.uuid4().hex[:8]
    connection = {
        "id": "slack-setup",
        "platform": "slack",
        # Exercise the same setup API without taking events from the running Socket Mode bot.
        "options": {"transport": "http"},
        "capture": {"enabled": True, "options": {"bot_token": token}},
        "new_channel_name": f"abx-setup-new-{suffix}",
        "saved_channel_name": f"abx-setup-saved-{suffix}",
    }
    response = console.post("/auth/login", json={"password": "test-console-password-123"})
    assert response.status_code == 200
    console.headers["x-csrf-token"] = response.json()["csrf"]
    saved = console.put("/api/settings", json={"connections": [connection]})
    assert saved.status_code == 200
    current = saved.json()["settings"]["connections"][0]
    ids = {field: current[field] for field in ("new_channel", "saved_channel")}
    status = console.get("/api/state").json()["connections"]["chat"]["slack-setup"]["roles"]["capture"]
    assert status["ok"], status
    assert all(ids.values()) and len(set(ids.values())) == 2
    async with aiohttp.ClientSession() as session:
        client = AsyncWebClient(token=token, session=session)
        for field, channel in ids.items():
            actual = (await client.conversations_info(channel=channel))["channel"]
            assert actual["name"] == connection[f"{field}_name"]
            assert actual["is_member"]
    with sqlite3.connect(tmp_path / "bridge.sqlite3") as db:
        persisted = Settings.model_validate_json(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
        assert persisted.connections[0].new_channel == ids["new_channel"]
        assert persisted.connections[0].saved_channel == ids["saved_channel"]
        assert len(db.execute("SELECT key FROM meta WHERE key LIKE 'sealed_cursor:%'").fetchall()) == 1
    # Force a real stop/start with the persisted destinations, then verify stable IDs/cursor.
    for enabled in (False, True):
        current["enabled"] = enabled
        response = console.put("/api/settings", json={"connections": [current]})
        assert response.status_code == 200
        current = response.json()["settings"]["connections"][0]
        assert {field: current[field] for field in ids} == ids
    with sqlite3.connect(tmp_path / "bridge.sqlite3") as db:
        assert len(db.execute("SELECT key FROM meta WHERE key LIKE 'sealed_cursor:%'").fetchall()) == 1
    assert console.get("/api/state").json()["connections"]["chat"]["slack-setup"]["roles"]["capture"]["ok"]
