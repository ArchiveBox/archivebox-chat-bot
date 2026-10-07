"""Explicit live Zulip acceptance, using private channels and real human messages.

Set ZULIP_TEST_CREDENTIALS to the mode-600 JSON containing zulip, zulip_ai,
and zulip_test (new_channel, saved_channel, stream_trigger_id, dm_trigger_id,
human_id). A human must send ten prior topic messages, then a capture-bot
mention and a DM containing the configured acceptance URL. Card verification
also requires ARCHIVEBOX_TEST_URL and ARCHIVEBOX_TEST_TOKEN_FILE pointing to
a real collection with a sealed screenshot/favicon capture.
"""

import asyncio
import io
import json
import os
import re
from pathlib import Path
from time import monotonic

import httpx
import pytest
from PIL import Image

from archivebox_slack.archivebox import ArchiveBox
from archivebox_slack.config import Settings
from archivebox_slack.engine import Engine
from archivebox_slack.store import Store
from archivebox_slack.text import size_label
from archivebox_slack.zulip import Zulip


class PrivateCredentials(dict):
    def __repr__(self):
        return "<private live credentials>"


@pytest.fixture
def credentials():
    path = Path(os.environ["ZULIP_TEST_CREDENTIALS"])
    assert path.stat().st_mode & 0o077 == 0, "Bot credentials must be private"
    return PrivateCredentials(json.loads(path.read_text()))


def settings_for(credentials):
    capture, ai, channels = credentials["zulip"], credentials["zulip_ai"], credentials["zulip_test"]
    return Settings(
        platform="zulip",
        zulip_url=capture["site"],
        zulip_email=capture["email"],
        zulip_api_key=capture["api_key"],
        zulip_ai_email=ai["email"],
        zulip_ai_api_key=ai["api_key"],
        new_channel=str(channels["new_channel"]),
        saved_channel=str(channels["saved_channel"]),
        archivebox_url=os.environ.get("ARCHIVEBOX_TEST_URL", "http://127.0.0.1:18997"),
        archivebox_public_url=os.environ.get("ARCHIVEBOX_TEST_URL", "http://127.0.0.1:18997"),
    )


async def get_message(bot, message_id, apply_markdown=False):
    response = await bot._api(
        "GET",
        "messages",
        {
            "message_ids": [int(message_id)],
            "apply_markdown": apply_markdown,
        },
    )
    assert len(response["messages"]) == 1
    return response["messages"][0]


async def private_channels(bot):
    for field in ("new_channel", "saved_channel"):
        channel_id = getattr(bot.settings, field)
        stream = (await bot._api("GET", f"streams/{channel_id}"))["stream"]
        assert stream["invite_only"], "Live tests may post only to private test channels"
        assert stream["name"].startswith("archivebox-") and stream["name"].endswith("-test")


async def test_real_bot_identities_and_private_setup(credentials):
    settings = settings_for(credentials)
    for role, credential in (("capture", "zulip"), ("ai", "zulip_ai")):
        bot = Zulip(settings, role)
        try:
            await private_channels(bot)
            identity = await bot.check()
            assert identity["bot_id"] == str(credentials[credential]["user_id"])
            assert identity["bot_name"]
            channels = await bot.setup_channels()
            assert channels == {key: str(credentials["zulip_test"][key]) for key in ("new_channel", "saved_channel")}
        finally:
            await bot.close()


@pytest.fixture
async def replayed(credentials, tmp_path):
    """Recover real server history through start(), then restart from its durable cursor."""
    settings = settings_for(credentials)
    triggers = credentials["zulip_test"]
    store = Store(tmp_path / "zulip-inbox")
    bot = Zulip(settings, store=store)
    cursor_key = f"zulip:{settings.zulip_url}:{credentials['zulip']['user_id']}:message_cursor"
    store.set_meta(cursor_key, min(int(triggers["stream_trigger_id"]), int(triggers["dm_trigger_id"])) - 1)
    messages = {}

    async def receive(message):
        store.enqueue(message.key, "message", message.as_dict())
        messages[message.ts] = message

    try:
        await private_channels(bot)
        await bot.start(receive)
        stream_message = messages[str(triggers["stream_trigger_id"])]
        dm_message = messages[str(triggers["dm_trigger_id"])]
        assert stream_message.is_mention and not stream_message.is_dm
        assert stream_message.thread == "Zulip live acceptance"
        assert dm_message.is_dm
        assert stream_message.user == dm_message.user == str(triggers["human_id"])
        assert (await bot.user(stream_message.user))["is_bot"] is False
        expected_url = triggers.get("acceptance_url", "https://example.com/?zulip-live-acceptance=20261007")
        assert expected_url in stream_message.text and expected_url in dm_message.text
        assert "@**ArchiveBox" not in stream_message.text
        for message in (stream_message, dm_message):
            assert store.get(message.key)["payload"] == message.as_dict()
        cursor = int(store.meta(cursor_key))
        assert cursor >= max(int(triggers["stream_trigger_id"]), int(triggers["dm_trigger_id"]))
        await bot.close()
        bot = Zulip(settings, store=store)
        messages.clear()
        await bot.start(receive)
        assert str(triggers["stream_trigger_id"]) not in messages
        assert str(triggers["dm_trigger_id"]) not in messages
        yield bot, stream_message, dm_message
    finally:
        await bot.close()
        store.close()


async def test_real_human_context_reactions_and_replies(replayed):
    bot, stream_message, dm_message = replayed
    context = await bot.recent(stream_message)
    assert len(context) == 10
    assert [text.split(": ", 1)[1] for text in context] == [
        f"Context {number}: checking the archive discussion history." for number in range(2, 12)
    ]
    for status, expected_code in (("runner", "1f3c3"), ("x", "274c"), ("white_check_mark", "2705")):
        await bot.react(stream_message, status)
        actual = await get_message(bot, stream_message.ts)
        codes = {reaction["emoji_code"] for reaction in actual["reactions"] if reaction["user_id"] == int(bot.bot_id)}
        assert expected_code in codes
        if status != "runner":
            assert "1f3c3" not in codes
    await bot._api(
        "DELETE",
        f"messages/{stream_message.ts}/reactions",
        {
            "emoji_name": "cross_mark",
            "emoji_code": "274c",
            "reaction_type": "unicode_emoji",
        },
    )
    for message in (stream_message, dm_message):
        text = "ArchiveBox native Zulip acceptance: authenticated reply verified."
        reply_id = await bot.post_text(message, text)
        reply = await get_message(bot, reply_id)
        assert reply["content"] == text
        assert reply["sender_id"] == int(bot.bot_id)
        assert reply["type"] == ("private" if message.is_dm else "stream")
        if not message.is_dm:
            assert str(reply["stream_id"]) == message.channel and reply["subject"] == message.thread


async def test_real_archive_card_and_native_uploaded_images(credentials):
    settings = settings_for(credentials)
    settings.archivebox_token = Path(os.environ["ARCHIVEBOX_TEST_TOKEN_FILE"]).read_text().strip()
    archive, bot = ArchiveBox(settings), Zulip(settings)
    try:
        await private_channels(bot)
        identity = await bot.check()
        page = await archive.snapshots(status="sealed", with_archiveresults=True, limit=100)
        snapshots = [
            row
            for row in page["items"]
            if {"screenshot", "favicon"}
            <= {result["plugin"] for result in row["archiveresults"] if result["status"] == "succeeded"}
        ]
        assert snapshots, "Produce a real sealed screenshot/favicon capture before running acceptance"
        snapshot = snapshots[0]
        media = {kind: await archive.artifact(snapshot, kind) for kind in ("screenshot", "favicon")}
        assert all(media.values())
        assert media["screenshot"][0].startswith(b"\x89PNG\r\n\x1a\n")
        assert all(value[1].startswith("image/") for value in media.values())
        detail_url = archive.detail_url(snapshot)
        card_id = await bot.post_card(snapshot, detail_url, media, f"zulip-live:{snapshot['id']}")
        card = await get_message(bot, card_id)
        assert str(card["stream_id"]) == settings.saved_channel
        assert str(card["sender_id"]) == identity["bot_id"]
        assert snapshot["url"] in card["content"] and detail_url in card["content"]
        assert size_label(snapshot["output_size"]) in card["content"]
        assert snapshot["persona"] in card["content"]
        assert "\n" not in card["content"] and "\r" not in card["content"]
        uploaded = {
            {"📷": "screenshot", "🌐": "favicon"}[label]: path
            for label, path in re.findall(r"\[([📷🌐])\]\(([^)]+)\)", card["content"])
        }
        assert set(uploaded) == {"screenshot", "favicon"}
        async with httpx.AsyncClient(
            auth=(settings.zulip_email, settings.zulip_api_key), follow_redirects=True
        ) as client:
            for kind, path in uploaded.items():
                assert path.startswith("/user_uploads/")
                response = await client.get(settings.zulip_url + path)
                response.raise_for_status()
                assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
                with Image.open(io.BytesIO(response.content)) as image:
                    assert image.format == "PNG" and image.mode == "RGBA"
                    bound = 1200 if kind == "screenshot" else 32
                    assert 0 < image.width <= bound and 0 < image.height <= bound
                    if kind == "screenshot":
                        assert any(low < high for low, high in image.getextrema()[:3])
                    with Image.open(io.BytesIO(media[kind][0])) as source:
                        original = source.convert("RGBA")
                        original.thumbnail((bound, bound))
                        assert original.size == image.size
                        assert original.tobytes() == image.tobytes()
        rendered = await get_message(bot, card_id, apply_markdown=True)
        assert "<a " in rendered["content"] and "/user_uploads/" in rendered["content"]
        assert snapshot["title"] in rendered["content"]
        assert rendered["content"].count('class="message_inline_image"') == 2
    finally:
        await archive.close()
        await bot.close()


async def test_real_deleted_queue_recovers_without_replaying_delivered_messages(credentials, tmp_path):
    settings = settings_for(credentials)
    store = Store(tmp_path / "zulip-queue-recovery")
    bot = Zulip(settings, store=store)
    seen = []

    async def receive(message):
        store.enqueue(message.key, "message", message.as_dict())
        seen.append(message.ts)

    try:
        await bot.start(receive)
        original_queue, cursor = bot.queue_id, bot.message_cursor
        assert original_queue and cursor > 0
        await bot._api("DELETE", "events", {"queue_id": original_queue})
        deadline = monotonic() + 15
        while bot.queue_id == original_queue:
            assert bot.task and not bot.task.done(), "The real event listener must remain running"
            assert monotonic() < deadline, "Deleted event queue was not recreated"
            await asyncio.sleep(0.1)
        assert bot.queue_id and bot.message_cursor >= cursor
        assert all(int(message_id) > cursor for message_id in seen)
    finally:
        await bot.close()
        store.close()


async def test_real_native_commands_and_disabled_command_filter(credentials, tmp_path):
    ids = credentials["zulip_commands_test"]
    settings = settings_for(credentials)
    settings.commands = ["help", "status", "save"]
    settings.enable_dms = settings.enable_mentions = settings.enable_new_urls = False
    store = Store(tmp_path / "zulip-native-commands")
    store.save_settings(settings)
    engine = Engine(store)
    bot = Zulip(settings, store=store)
    engine.bots["capture"] = bot
    cursor_key = f"zulip:{settings.zulip_url}:{credentials['zulip']['user_id']}:message_cursor"
    store.set_meta(cursor_key, min(int(ids[f"{command}_id"]) for command in ("help", "status", "search", "save")) - 1)
    messages = {}

    async def receive(message):
        messages[message.ts] = message
        await engine.receive(message)

    try:
        await private_channels(bot)
        await bot.start(receive)
        for command in ("help", "status", "search", "save"):
            message = messages[str(ids[f"{command}_id"])]
            assert message.command == command
            assert message.is_dm or message.is_mention
            assert "@**ArchiveBox" not in message.text
            row = store.get(engine.scope("capture") + ":" + message.key)
            if command == "search":
                assert message.text == "archivebox.io" and row is None
            else:
                assert row and row["state"] == "queued" and row["payload"]["command"] == command
            if command in ("help", "status"):
                assert message.text == ""
            if command == "save":
                assert message.text == ids.get("save_url", "https://archivebox.io/?zulip-png-command=20261007")
    finally:
        await bot.close()
        store.close()
