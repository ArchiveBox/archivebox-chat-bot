"""Explicit live Beeper acceptance; never included in the default offline suite.

Readiness: BEEPER_TEST_URL=http://127.0.0.1:23484 uv run --all-groups python -m
pytest tests/test_beeper_live.py -k readiness -q

Authenticated tests require BEEPER_TEST_CREDENTIALS: a protected JSON file with
base_url, access_token, account_id, chat_ids (dedicated acceptance conversations).
The send test additionally requires BEEPER_TEST_ALLOW_SEND=1. Missing prerequisites
fail rather than skip or report a simulated success.
"""

import asyncio
import json
import os
import stat
import uuid
from pathlib import Path

import httpx
import pytest

from archivebox_chat_bot.transports.beeper import BeeperTransport


async def test_real_server_readiness_contract():
    async with httpx.AsyncClient(base_url=os.environ["BEEPER_TEST_URL"]) as client:
        info = await client.get("/v1/info")
        assert info.status_code == 200
        assert info.json()["server"]["status"] == "running"
        setup = await client.get("/v1/app/setup")
        assert setup.status_code == 200
        assert setup.json()["state"] == "needs-login"
        bridges = await client.get("/v1/bridges")
        assert bridges.status_code == 401
        spec = await client.get("/v1/spec")
        assert spec.status_code == 200
        schema = spec.json()
        assert "post" in schema["paths"]["/v1/bridges/{bridgeID}/login-sessions"]
        assert "get" in schema["paths"]["/v1/chats/{chatID}/messages/{messageID}"]
        assert schema["components"]["schemas"]["SendStatus"]["properties"]["status"]["enum"] == [
            "SUCCESS",
            "PENDING",
            "FAIL_RETRIABLE",
            "FAIL_PERMANENT",
        ]


async def test_real_transport_readiness_requires_login_without_advancing(tmp_path):
    events = asyncio.Queue()
    transport = BeeperTransport(
        {"base_url": os.environ["BEEPER_TEST_URL"], "account_id": "matrix"},
        "readiness",
        tmp_path,
        events.put,
    )
    try:
        with pytest.raises(RuntimeError, match="Beeper needs sign-in"):
            await transport.start()
        assert transport.task is None
        assert not transport.path.exists()
        assert events.empty()
    finally:
        await transport.close()


def credentials():
    path = Path(os.environ["BEEPER_TEST_CREDENTIALS"])
    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0, "Protect live API credentials with mode 600"
    options = json.loads(path.read_text())
    assert options["access_token"] and options["account_id"] and options["chat_ids"]
    return options


async def test_real_account_identity_selection_and_restart(tmp_path):
    options = credentials()
    events = asyncio.Queue()
    # An account may be checked and its picker populated without listening to any chat.
    transport = BeeperTransport({**options, "chat_ids": []}, "acceptance", tmp_path, events.put)
    try:
        identity = await transport.check()
        available = await transport.channels()
        assert set(options["chat_ids"]) <= {c["id"] for c in available}
        await transport.start()
        await transport._reconcile()
        assert events.empty()
        assert transport.state["cursors"] == {}
        with pytest.raises(ValueError, match="Select this Beeper conversation"):
            await transport.history(options["chat_ids"][0])
    finally:
        await transport.close()
    selected = BeeperTransport(options, "acceptance", tmp_path, events.put)
    try:
        assert (await selected.check())["id"] == identity["id"]
        old_ids = {m["id"] for m in await selected.history(options["chat_ids"][0])}
        await selected.start()
        assert not old_ids.intersection({events.get_nowait()["id"] for _ in range(events.qsize())})
        assert set(selected.state["selected"]) == set(options["chat_ids"])
        saved = json.loads(selected.path.read_text())
        assert saved["identity"] == selected.identity["user"]["id"]
        assert stat.S_IMODE(selected.path.stat().st_mode) == 0o600
    finally:
        await selected.close()
    restarted = BeeperTransport(options, "acceptance", tmp_path, events.put)
    try:
        assert (await restarted.check())["id"] == identity["id"]
        assert restarted.state["selected"] == saved["selected"]
        assert restarted.state["cursors"] == saved["cursors"]
    finally:
        await restarted.close()


async def test_real_send_confirms_upstream_success(tmp_path):
    assert os.environ["BEEPER_TEST_ALLOW_SEND"] == "1"
    options = credentials()
    channel = options["chat_ids"][0]
    events = asyncio.Queue()
    transport = BeeperTransport({**options, "include_own_messages": True}, "acceptance:capture", tmp_path, events.put)
    try:
        await transport.start()
        text = "ArchiveBox Beeper delivery acceptance " + uuid.uuid4().hex
        message_id = await transport.send(channel, text)
        message = await transport._request(
            "GET", f"/v1/chats/{transport._part(channel)}/messages/{transport._part(message_id)}"
        )
        assert message["sendStatus"]["status"] == "SUCCESS"
        assert message["text"] == text
        assert message["accountID"] == options["account_id"]
        assert message["isSender"] is True
        await transport._reconcile()
        assert message_id not in {events.get_nowait()["id"] for _ in range(events.qsize())}
    finally:
        await transport.close()
