"""Real Ergo acceptance. Start Ergo and set IRC_TEST_PORT (default 16667)."""

import asyncio
import os
import uuid

import pytest

from archivebox_chat_bot.transports.irc import IRCTransport


async def test_real_irc_identity_delivery_and_restart(tmp_path):
    suffix = uuid.uuid4().hex[:8]
    options = {"server": "127.0.0.1", "port": int(os.environ.get("IRC_TEST_PORT", "16667")), "tls": False}
    received, peer_received = asyncio.Queue(), asyncio.Queue()
    bot = IRCTransport({**options, "nickname": "bot" + suffix}, "bot", tmp_path, received.put)
    peer = IRCTransport({**options, "nickname": "human" + suffix}, "peer", tmp_path, peer_received.put)
    channel = "#archivebox-" + suffix
    try:
        await bot.start()
        identity = await bot.check()
        assert identity["name"] == "bot" + suffix
        assert identity["capabilities"]["authenticated_users"] is True
        assert identity["capabilities"]["reactions"] is False
        await peer.start()
        assert (await bot.join(channel))["id"] == channel
        await peer.join(channel)
        await peer.send(channel, "bot" + suffix + " https://example.com/irc")
        event = await asyncio.wait_for(received.get(), 10)
        assert event["channel"] == channel
        assert event["text"] == "bot" + suffix + " https://example.com/irc"
        assert event["is_mention"] and not event["is_dm"]
        assert event["user"].startswith("anonymous:")
        assert event["id"] and not event["id"].startswith(bot.session)
        await bot.send(peer.client.nickname, "saved https://example.com/irc")
        reply = await asyncio.wait_for(peer_received.get(), 10)
        assert reply["is_dm"] and reply["channel"] == bot.client.nickname
        assert reply["text"] == "saved https://example.com/irc"
        assert received.empty()  # own echo is ignored
        old_id = event["id"]
        await bot.close()
        bot = IRCTransport({**options, "nickname": "bot" + suffix}, "bot", tmp_path, received.put)
        await bot.start()
        await bot.join(channel)
        await peer.send(channel, "restart-message")
        event = await asyncio.wait_for(received.get(), 10)
        assert event["text"] == "restart-message"
        assert event["id"] != old_id
        with pytest.raises(NotImplementedError):
            await bot.react(channel, event["id"], "done")
        with pytest.raises(ValueError):
            await bot.send(channel, "photo", media=[{"path": "/tmp/photo.png"}])
    finally:
        await bot.close()
        await peer.close()


async def test_real_sasl_identity_survives_nickname_change(tmp_path):
    suffix = uuid.uuid4().hex[:8]
    nickname = "auth" + suffix
    password = uuid.uuid4().hex
    port = int(os.environ.get("IRC_TEST_PORT", "16667"))
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(f"NICK {nickname}\r\nUSER test 0 * :Acceptance\r\n".encode())
        await writer.drain()
        async with asyncio.timeout(10):
            while b" 376 " not in await reader.readline():
                pass
        writer.write(f"PRIVMSG NickServ :REGISTER {password}\r\n".encode())
        await writer.drain()
        async with asyncio.timeout(10):
            while b"You're now logged in as" not in await reader.readline():
                pass
    finally:
        writer.close()
        await writer.wait_closed()
    options = {"server": "127.0.0.1", "port": port, "tls": False}
    events, peer_events = asyncio.Queue(), asyncio.Queue()
    bot = IRCTransport({**options, "nickname": "receiver" + suffix}, "bot", tmp_path, events.put)
    peer = IRCTransport(
        {**options, "nickname": nickname, "sasl_username": nickname, "sasl_password": password},
        "peer",
        tmp_path,
        peer_events.put,
    )
    channel = "#sasl-" + suffix
    try:
        await bot.start()
        await peer.start()
        assert peer.client.authenticated
        await bot.join(channel)
        await peer.join(channel)
        await peer.send(channel, "authenticated")
        first = await asyncio.wait_for(events.get(), 10)
        assert first["user"] == "account:" + nickname
        await peer.client.set_nickname("renamed" + suffix)
        await peer.send(channel, "renamed")
        second = await asyncio.wait_for(events.get(), 10)
        assert second["user"] == first["user"]
        assert second["user_name"] == "renamed" + suffix
    finally:
        await bot.close()
        await peer.close()


async def test_real_irc_refuses_invalid_sasl(tmp_path):
    events = asyncio.Queue()
    bot = IRCTransport(
        {
            "server": "127.0.0.1",
            "port": int(os.environ.get("IRC_TEST_PORT", "16667")),
            "tls": False,
            "nickname": "bad" + uuid.uuid4().hex[:8],
            "sasl_username": "missing" + uuid.uuid4().hex[:8],
            "sasl_password": "invalid-password",
        },
        "invalid",
        tmp_path,
        events.put,
    )
    try:
        with pytest.raises(RuntimeError, match="connection failed"):
            await bot.start()
        assert not bot.client.connected
        assert events.empty()
    finally:
        await bot.close()


async def test_real_irc_without_message_tags_uses_unique_session_ids(tmp_path):
    suffix = uuid.uuid4().hex[:8]
    options = {"server": "127.0.0.1", "port": int(os.environ.get("IRC_TEST_PORT", "16667")), "tls": False}
    received, peer_received = asyncio.Queue(), asyncio.Queue()
    bot_options = {**options, "nickname": "legacy" + suffix}
    bot = IRCTransport(bot_options, "bot", tmp_path, received.put)
    peer = IRCTransport({**options, "nickname": "source" + suffix}, "peer", tmp_path, peer_received.put)
    ids = []
    try:
        await peer.start()
        for _ in range(2):
            await bot.start()
            await bot.client.rawmsg("CAP", "REQ", "-message-tags")
            async with asyncio.timeout(10):
                while bot.client._capabilities.get("message-tags"):
                    await asyncio.sleep(0.01)
            await peer.send(bot.client.nickname, "same text without upstream msgid")
            event = await asyncio.wait_for(received.get(), 10)
            assert event["id"].startswith(bot.session + ":")
            ids.append(event["id"])
            await bot.close()
            bot = IRCTransport(bot_options, "bot", tmp_path, received.put)
        assert ids[0] != ids[1]
    finally:
        await bot.close()
        await peer.close()
