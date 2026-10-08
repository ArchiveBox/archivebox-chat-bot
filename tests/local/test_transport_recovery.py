import asyncio
import socket

from archivebox_chat_bot.transports.beeper import BeeperTransport


async def test_beeper_starts_a_recovery_poller_after_a_real_connection_refusal(tmp_path):
    # Bind an OS-assigned loopback port, then release it so the HTTP client
    # exercises an actual refused TCP connection rather than a fake response.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]

    events = asyncio.Queue()
    transport = BeeperTransport(
        {"base_url": f"http://127.0.0.1:{port}", "account_id": "offline-account"},
        "recovery-test",
        tmp_path,
        events.put,
    )
    try:
        await transport.start()
        assert transport.recovering
        assert transport.task is not None and not transport.task.done()
        assert transport.identity is None
        assert "identity" not in transport.state
        assert (await transport.check())["state"] == "recovering"
        assert events.empty()
    finally:
        await transport.close()
