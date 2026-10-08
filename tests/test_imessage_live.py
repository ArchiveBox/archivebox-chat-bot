"""Real macOS imsg acceptance; requires FDA and IMESSAGE_TEST_CHAT_ID=self chat.

IMESSAGE_TEST_IMSG selects a signed imsg executable (default imsg).
Run explicitly; a missing OS grant is a failure, never a simulated pass.
"""

import asyncio
import os
import uuid

from archivebox_chat_bot.transports.imessage import IMessageTransport


async def test_real_imessage_self_send_and_watch(tmp_path):
    channel = os.environ["IMESSAGE_TEST_CHAT_ID"]
    events = asyncio.Queue()
    transport = IMessageTransport(
        {"imsg_path": os.environ.get("IMESSAGE_TEST_IMSG", "imsg")}, "acceptance", tmp_path, events.put
    )
    try:
        identity = await transport.check()
        assert identity["version"]
        assert identity["capabilities"]["reactions"] is False
        assert (await transport.join(channel))["id"] == channel
        await transport.start()
        text = "ArchiveBox iMessage self-test " + uuid.uuid4().hex
        guid = await transport.send(channel, text)
        history = await transport._rpc("messages.history", {"chat_id": int(channel), "limit": 10})
        matches = [message for message in history["messages"] if message["guid"] == guid]
        assert len(matches) == 1
        assert matches[0]["text"] == text
        assert matches[0]["is_from_me"]
        async with asyncio.timeout(10):
            while transport.cursor.get("guid") != guid:
                await asyncio.sleep(0.1)
        assert events.empty()  # the bot's own outgoing message must never re-enter core
        assert transport.cursor_path.exists()
        await transport.close()
        restarted = IMessageTransport(transport.options, "acceptance", tmp_path, events.put)
        try:
            await restarted.start()  # verifies stored cursor belongs to this real database
            assert restarted.cursor["guid"] == guid
        finally:
            await restarted.close()
    finally:
        await transport.close()
