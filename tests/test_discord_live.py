"""Read-only acceptance against the single running Discord Gateway service.

DISCORD_TEST_CREDENTIALS=/tmp/archivebox-discord-credentials.json
DISCORD_TEST_DATA_DIR=/path/to/running/data
uv run python -m pytest -xq tests/test_discord_live.py

Credentials must be mode 0600 JSON: capture and ai each contain bot_token and
id; guild_id, user_id and connection_id identify the real installation.
Cases contain role (capture/ai), kind (mention/dm), channel_id and message_id
for actual human triggers already completed by the running service. Capture
cases also require urls and expected_tags lists. Include both capture mention
and DM plus at least one AI case. Enable screenshot previews before capturing.
These tests never send messages, open another Gateway, or change configuration.
"""

import json
import os
import sqlite3
from pathlib import Path

import httpx
import pytest

from archivebox_chat_bot.archivebox import ArchiveBox
from archivebox_chat_bot.config import Settings
from archivebox_chat_bot.text import size_label


class PrivateCredentials(dict):
    def __repr__(self):
        return "<private Discord credentials>"


@pytest.fixture
def credentials():
    path = Path(os.environ["DISCORD_TEST_CREDENTIALS"])
    assert path.stat().st_mode & 0o077 == 0, "Credentials require owner-only permissions"
    return PrivateCredentials(json.loads(path.read_text()))


async def discord_api(credential, path):
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                "https://discord.com/api/v10" + path,
                headers={"Authorization": "Bot " + credential["bot_token"]},
            )
    except httpx.HTTPError:
        pytest.fail("Discord REST transport failed", pytrace=False)
    assert response.status_code == 200, f"Discord REST returned HTTP {response.status_code}"
    return response.json()


@pytest.fixture
def runtime():
    directory = Path(os.environ["DISCORD_TEST_DATA_DIR"])
    with sqlite3.connect(f"file:{directory / 'bridge.sqlite3'}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        settings = Settings.model_validate_json(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
        jobs = [dict(row) for row in db.execute("SELECT * FROM jobs")]
    for job in jobs:
        job["payload"] = json.loads(job["payload"])
        job["result"] = json.loads(job["result"] or "{}")
    return settings, jobs


async def test_real_discord_identity_installation_and_commands(credentials):
    identities = []
    for role in ("capture", "ai"):
        credential = credentials[role]
        identity = await discord_api(credential, "/users/@me")
        assert identity["bot"] is True
        assert identity["id"] == credential["id"]
        guilds = await discord_api(credential, "/users/@me/guilds")
        assert credentials["guild_id"] in {guild["id"] for guild in guilds}
        identities.append(identity["id"])
    assert len(set(identities)) == 2
    app = await discord_api(credentials["capture"], "/oauth2/applications/@me")
    commands = await discord_api(
        credentials["capture"], f"/applications/{app['id']}/guilds/{credentials['guild_id']}/commands"
    )
    command = next(row for row in commands if row["name"] == "archivebox")
    assert {row["name"] for row in command["options"]} == {"save", "search", "auto", "status", "help"}


async def test_real_discord_human_capture_cards_and_ai(credentials, runtime):
    settings, jobs = runtime
    connection = next(c for c in settings.connections if c.id == credentials["connection_id"])
    assert connection.platform == "discord"
    assert connection.options["guild_id"] == credentials["guild_id"]
    assert connection.upload_images and connection.saved_channel
    cases = credentials["cases"]
    assert {case["kind"] for case in cases if case["role"] == "capture"} >= {"mention", "dm"}
    assert any(case["role"] == "ai" for case in cases)
    archive = ArchiveBox(settings)
    try:
        for case in cases:
            role = case["role"]
            credential = credentials[role]
            actual = await discord_api(credential, f"/channels/{case['channel_id']}/messages/{case['message_id']}")
            assert actual["author"]["id"] == credentials["user_id"]
            assert not actual["author"].get("bot", False)
            matching = [
                job
                for job in jobs
                if job["kind"] == "message"
                and job["payload"].get("connection") == connection.id
                and job["payload"].get("role") == role
                and job["payload"].get("ts") == case["message_id"]
            ]
            assert len(matching) == 1, "Human trigger must produce exactly one durable job"
            job = matching[0]
            assert job["state"] == "done"
            assert job["payload"]["channel"] == case["channel_id"]
            assert job["payload"]["user"] == credentials["user_id"]
            assert job["payload"]["is_dm"] == (case["kind"] == "dm")
            if case["kind"] == "mention":
                assert job["payload"]["is_mention"]
                assert credential["id"] in {user["id"] for user in actual["mentions"]}
            reactions = {row["emoji"]["name"]: row for row in actual.get("reactions", [])}
            assert reactions["✅"]["me"] is True
            assert not reactions.get("🏃", {}).get("me", False)
            result = job["result"]
            if role == "ai":
                assert result["session"]["id"] and result["answer"]
                answer = await discord_api(
                    credential, f"/channels/{case['channel_id']}/messages/{result['message_id']}"
                )
                assert answer["author"]["id"] == credential["id"]
                final_offset = ((len(result["answer"]) - 1) // 2000) * 2000
                assert answer["content"] == result["answer"][final_offset:]
                continue
            assert set(result["urls"]) == set(case["urls"])
            assert set(result["tags"]) == set(case["expected_tags"])
            crawl = await archive.crawl(result["crawl_id"])
            assert crawl["status"] == "sealed"
            snapshots = await archive.crawl_snapshots(result["crawl_id"])
            assert {snapshot["url"] for snapshot in snapshots} == set(case["urls"])
            for snapshot in snapshots:
                assert snapshot["status"] == "sealed"
                assert set(case["expected_tags"]) == set(snapshot["tags"])
                assert await archive.artifact(snapshot, "screenshot"), "Capture must save an actual screenshot"
                announcements = [
                    row
                    for row in jobs
                    if row["kind"] == "announcement"
                    and row["payload"].get("id") == snapshot["id"]
                    and row["scope"] == job["scope"]
                ]
                assert len(announcements) == 1, "Snapshot must produce exactly one saved announcement"
                announcement = announcements[0]
                assert announcement["state"] == "done"
                card = await discord_api(
                    credentials["capture"],
                    f"/channels/{connection.saved_channel}/messages/{announcement['result']['message_id']}",
                )
                assert card["author"]["id"] == credentials["capture"]["id"]
                assert len(card["embeds"]) == 1
                embed = card["embeds"][0]
                assert snapshot["url"] in embed["description"]
                assert archive.detail_url(snapshot) in embed["description"]
                assert "Saved " + size_label(snapshot["output_size"]) in embed["description"]
                assert embed["thumbnail"]["width"] > 0 and embed["thumbnail"]["height"] > 0
                assert any(item["filename"] == "screenshot.png" and item["size"] > 0 for item in card["attachments"])
    finally:
        await archive.close()
