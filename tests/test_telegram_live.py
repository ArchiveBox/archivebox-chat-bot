"""Real Telegram acceptance without starting a competing getUpdates consumer.

TELEGRAM_TEST_CREDENTIALS: private JSON with telegram, telegram_ai (bot_token,
username, bot_id), and telegram_test (group_id, group_name). Read-only tests
can run alongside the service. The connector identity test uses webhook mode
locally and never registers a webhook or consumes updates.

The running-inbox test additionally needs TELEGRAM_TEST_DATA_DIR and real
human_id, group_trigger_id, dm_trigger_id, group_context_ids, group_urls, and
dm_urls in telegram_test.
The send/upload/reaction test requires TELEGRAM_TEST_ALLOW_SEND=1 and uploads a
real screenshot from the running ArchiveBox collection to the private group.
"""

import json
import os
import secrets
import sqlite3
from pathlib import Path

import httpx
import pytest

from archivebox_chat_bot.archivebox import ArchiveBox
from archivebox_chat_bot.config import Settings
from archivebox_chat_bot.connectors import ConnectorWorker, TransportBot
from archivebox_chat_bot.store import Store
from archivebox_chat_bot.text import size_label


class PrivateCredentials(dict):
    def __repr__(self):
        return "<private live credentials>"


@pytest.fixture
def credentials():
    path = Path(os.environ["TELEGRAM_TEST_CREDENTIALS"])
    assert path.stat().st_mode & 0o077 == 0, "Credentials must have owner-only permissions"
    return PrivateCredentials(json.loads(path.read_text()))


async def telegram_api(credential, method, **params):
    """Never expose token-bearing HTTP URLs through assertion/transport failures."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"https://api.telegram.org/bot{credential['bot_token']}/{method}", json=params)
    except httpx.HTTPError:
        pytest.fail(f"Telegram {method} transport failed", pytrace=False)
    assert response.status_code == 200, f"Telegram {method} returned HTTP {response.status_code}"
    payload = response.json()
    assert payload["ok"] is True, f"Telegram {method} failed"
    return payload["result"]


async def private_group(credentials):
    setup = credentials["telegram_test"]
    group = await telegram_api(credentials["telegram"], "getChat", chat_id=setup["group_id"])
    assert group["type"] in {"group", "supergroup"}
    assert group["title"] == setup["group_name"]
    assert "archivebox" in group["title"].lower() and "test" in group["title"].lower()
    assert not group.get("username"), "Acceptance must use a private group"
    return group


async def test_real_bot_identities_and_private_group(credentials):
    group = await private_group(credentials)
    identities = []
    for key in ("telegram", "telegram_ai"):
        credential = credentials[key]
        identity = await telegram_api(credential, "getMe")
        assert identity["is_bot"] is True
        assert str(identity["id"]) == credential["bot_id"]
        assert identity["username"] == credential["username"]
        assert identity["can_read_all_group_messages"] is True
        member = await telegram_api(credential, "getChatMember", chat_id=group["id"], user_id=identity["id"])
        assert member["status"] in {"administrator", "member"}
        identities.append(identity["id"])
    assert identities[0] != identities[1]


@pytest.fixture
async def connector(credentials, tmp_path):
    worker = ConnectorWorker()
    await worker.start()
    try:
        accounts = [
            {
                "id": f"telegram-acceptance:{role}",
                "platform": "telegram",
                "options": {
                    "bot_token": credentials[key]["bot_token"],
                    "username": credentials[key]["username"],
                    "mode": "webhook",
                    "webhook_secret": secrets.token_urlsafe(32),
                },
                "data_dir": str(tmp_path / role),
            }
            for role, key in (("capture", "telegram"), ("ai", "telegram_ai"))
        ]
        configured = await worker.call("configure", params={"accounts": accounts})
        assert all(value["state"] == "connected" for value in configured.values())
        yield worker, PrivateCredentials({"accounts": accounts})
    finally:
        await worker.close()


async def test_real_connector_identity_without_polling(credentials, connector):
    worker, _ = connector
    for role, key in (("capture", "telegram"), ("ai", "telegram_ai")):
        status = await worker.call("check", f"telegram-acceptance:{role}")
        assert status["state"] == "connected"
        assert status["bot_user_id"] == credentials[key]["bot_id"]
        assert status["username"] == credentials[key]["username"]
        assert status["transport"] == "webhook"
        assert status["capabilities"]["media"] and status["capabilities"]["reactions"]


async def test_real_reconfigure_preserves_unchanged_accounts(connector):
    worker, config = connector
    capture, ai = "telegram-acceptance:capture", "telegram-acceptance:ai"
    before = dict(worker.status)
    result = await worker.call("configure", params={"accounts": list(reversed(config["accounts"]))})
    assert set(result) == {capture, ai}
    assert worker.status[capture] is before[capture], "Unchanged capture adapter reconnected"
    assert worker.status[ai] is before[ai], "Unchanged AI adapter reconnected"
    # Remove one live adapter; the other must remain connected without a new status event.
    capture_config = config["accounts"][0]
    await worker.call("configure", params={"accounts": [capture_config]})
    assert worker.status[capture] is before[capture]
    assert (await worker.call("check", capture))["state"] == "connected"
    with pytest.raises(RuntimeError, match="Unknown account"):
        await worker.call("check", ai)
    # Rotating this account's webhook secret is a real configuration change.
    changed = {**capture_config, "options": {**capture_config["options"], "webhook_secret": secrets.token_urlsafe(32)}}
    await worker.call("configure", params={"accounts": [changed]})
    assert worker.status[capture] is not before[capture]
    assert (await worker.call("check", capture))["state"] == "connected"


def test_running_telegram_ingress_and_context(credentials):
    """Inspect real persisted human deliveries from the single running service."""
    setup = credentials["telegram_test"]
    directory = Path(os.environ["TELEGRAM_TEST_DATA_DIR"])
    with sqlite3.connect(f"file:{directory / 'bridge.sqlite3'}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute(
            "SELECT * FROM history WHERE role='capture' AND user_id=? ORDER BY seq", (setup["human_id"],)
        ).fetchall()
        jobs = db.execute("SELECT state,payload,result FROM jobs WHERE kind='message'").fetchall()
    group = next(row for row in rows if row["message_id"] == setup["group_trigger_id"])
    dm = next(row for row in rows if row["message_id"] == setup["dm_trigger_id"])
    assert group["channel"] == setup["group_id"]
    assert group["thread"] == f"telegram:{setup['group_id']}"
    assert dm["thread"] == f"telegram:{dm['channel']}"
    assert not group["is_bot"] and not dm["is_bot"]
    for trigger, expected in ((group, setup["group_urls"]), (dm, setup["dm_urls"])):
        job = next(row for row in jobs if json.loads(row["payload"])["ts"] == trigger["message_id"])
        assert job["state"] == "done"
        assert set(json.loads(job["result"])["urls"]) == set(expected)
    context = [
        row
        for row in rows
        if row["connection"] == group["connection"]
        and row["channel"] == group["channel"]
        and row["thread"] == group["thread"]
        and row["seq"] < group["seq"]
    ][-10:]
    assert [row["message_id"] for row in context] == setup["group_context_ids"]
    assert 0 < len(context) <= 10
    assert all(any(url in row["text"] for row in context) for url in setup["group_urls"])
    account_dir = directory / "accounts" / f"{group['connection']}:capture"
    with sqlite3.connect(f"file:{account_dir / 'connector.sqlite3'}?mode=ro", uri=True) as db:
        pending = [json.loads(row[0])["message"]["id"] for row in db.execute("SELECT payload FROM ingress")]
        checkpoint = db.execute("SELECT value FROM kv WHERE key LIKE 'telegram:polling:%'").fetchone()
    assert group["message_id"] not in pending and dm["message_id"] not in pending
    assert checkpoint and json.loads(checkpoint[0])["offset"] > 0


async def test_real_archive_card_native_image_and_reactions(credentials, connector, tmp_path):
    worker, _ = connector
    assert os.environ.get("TELEGRAM_TEST_ALLOW_SEND") == "1", "Explicitly enable messages to the private test group"
    group = await private_group(credentials)
    directory = Path(os.environ["TELEGRAM_TEST_DATA_DIR"])
    with sqlite3.connect(f"file:{directory / 'bridge.sqlite3'}?mode=ro", uri=True) as db:
        settings = Settings.model_validate_json(db.execute("SELECT value FROM settings").fetchone()[0])
    connection = next(value for value in settings.connections if value.platform == "telegram")
    connection = connection.model_copy(update={"id": "telegram-acceptance", "saved_channel": str(group["id"])})
    store = Store(tmp_path / "card")
    archive = ArchiveBox(settings)
    bot = TransportBot(connection, "capture", store, worker)
    account = "telegram-acceptance:capture"
    try:
        await bot.check()
        page = await archive.snapshots(status="sealed", with_archiveresults=True, limit=100)
        snapshot = next(row for row in page["items"] if row["url"] in credentials["telegram_test"]["group_urls"])
        screenshot = await archive.artifact(snapshot, "screenshot")
        assert screenshot and screenshot[1].startswith("image/")
        detail_url = archive.detail_url(snapshot)
        sent_id = await bot.post_card(
            snapshot, detail_url, {"screenshot": screenshot}, f"telegram-live:{snapshot['id']}"
        )
    finally:
        await archive.close()
        store.close()
    assert sent_id
    # Telegram has no getMessage API. Forwarding within the private test group
    # returns the actual stored media and original caption without replacing it.
    actual = await telegram_api(
        credentials["telegram"],
        "forwardMessage",
        chat_id=group["id"],
        from_chat_id=group["id"],
        message_id=int(sent_id.rsplit(":", 1)[-1]),
    )
    assert actual["photo"] and actual["photo"][-1]["width"] > 0
    assert "\n" not in actual["caption"]
    assert snapshot["url"] in actual["caption"]
    assert size_label(snapshot["output_size"]) in actual["caption"]
    assert snapshot["persona"] in actual["caption"]
    assert detail_url in {entity.get("url") for entity in actual["caption_entities"]}
    assert actual["from"]["id"] == int(credentials["telegram"]["bot_id"])
    for status in ("runner", "white_check_mark", "x"):
        assert await worker.call(
            "react", account, {"channel": str(group["id"]), "message_id": sent_id, "status": status}
        ) == {"ok": True}
