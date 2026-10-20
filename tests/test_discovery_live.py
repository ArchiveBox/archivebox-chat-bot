"""Real discovery acceptance against disposable ArchiveBox and Ergo servers.

Set ARCHIVEBOX_TEST_URL, ARCHIVEBOX_TEST_TOKEN_FILE, and IRC_TEST_PORT. The
ArchiveBox collection must contain exactly one sealed capture with saved outputs.
The IRC clients authenticate their identities but never send chat messages.
"""

import asyncio
import os
from pathlib import Path
from uuid import uuid4

from archivebox_chat_bot.archivebox import ArchiveBox
from archivebox_chat_bot.config import Connection, Settings
from archivebox_chat_bot.connectors import TransportBot
from archivebox_chat_bot.engine import Engine
from archivebox_chat_bot.store import Store


async def test_discovery_hydrates_only_new_scoped_announcements(tmp_path):
    settings = Settings(
        archivebox_url=os.environ["ARCHIVEBOX_TEST_URL"],
        archivebox_token=Path(os.environ["ARCHIVEBOX_TEST_TOKEN_FILE"]).read_text().strip(),
    )
    archive = ArchiveBox(settings)
    store = Store(tmp_path)
    bots = []
    traffic = []

    async def record_response(response):
        await response.aread()
        traffic.append((response.request.url, response.json(), len(response.content)))

    try:
        page = await archive.snapshots(status="sealed", with_archiveresults="true")
        assert page["total_items"] == 1
        snapshot = page["items"][0]
        assert snapshot["persona"] and snapshot["title"]
        assert any(result["output_files"] for result in snapshot["archiveresults"])
        archive._api.event_hooks["response"].append(record_response)

        for scope_number in range(2):
            connection = Connection(
                id=f"discovery-{scope_number}",
                platform="irc",
                saved_channel=f"#discovery-{scope_number}",
                options={
                    "server": "127.0.0.1",
                    "port": int(os.environ["IRC_TEST_PORT"]),
                    "tls": False,
                    "nickname": "discover" + uuid4().hex[:8],
                },
            )
            incoming = asyncio.Queue()
            bot = TransportBot(connection, "capture", store)
            bots.append(bot)
            await bot.start(incoming.put)
            assert bot.identity["user_id"]
            engine = Engine(store, settings, connection, None)
            engine.archive = archive
            engine.bots["capture"] = bot
            cursor = "sealed_cursor:" + engine.scope()
            store.set_meta(cursor, snapshot["modified_at"])
            traffic.clear()
            await engine.discover()
            key = f"saved:{engine.scope()}:{snapshot['id']}"
            job = store.get(key)
            assert job["payload"] == {**snapshot, "connection": connection.id}
            assert job["state"] == "queued"
            assert traffic[0][0].params.get("with_archiveresults") != "true"
            assert traffic[0][1]["items"][0]["archiveresults"] == []
            assert [url.path for url, _, _ in traffic] == [
                "/api/v1/core/snapshots",
                f"/api/v1/core/snapshot/{snapshot['id']}",
            ]
            frozen_payload = job["payload"]
            for state in ("queued", "uncertain", "done"):
                store.update(key, state)
                # Exercise the real overlap query even when fixture setup took
                # longer than the production two-minute overlap interval.
                store.set_meta(cursor, snapshot["modified_at"])
                traffic.clear()
                await engine.discover()
                assert [url.path for url, _, _ in traffic] == ["/api/v1/core/snapshots"]
                assert traffic[0][1]["items"][0]["archiveresults"] == []
                assert store.get(key)["payload"] == frozen_payload
                assert store.get(key)["state"] == state
            assert incoming.empty()
        assert len(store.jobs(limit=10)) == 2

        # A real API mutation between the list response and detail read must
        # not announce a capture that is no longer sealed.
        connection.saved_channel = "#discovery-paused"
        key = f"saved:{engine.scope()}:{snapshot['id']}"
        store.set_meta("sealed_cursor:" + engine.scope(), snapshot["modified_at"])

        async def pause_after_listing(response):
            if response.request.url.path == "/api/v1/core/snapshots":
                paused = await archive._request(
                    "PATCH", f"/api/v1/core/snapshot/{snapshot['id']}", json={"status": "paused"}
                )
                assert paused["status"] == "paused"

        archive._api.event_hooks["response"].append(pause_after_listing)
        try:
            await engine.discover()
            assert not store.has_job(key)
        finally:
            archive._api.event_hooks["response"].remove(pause_after_listing)
            sealed = await archive._request(
                "PATCH", f"/api/v1/core/snapshot/{snapshot['id']}", json={"action": "cancel"}
            )
            assert sealed["status"] == "sealed"

        await engine.discover()
        recovered = store.get(key)
        assert recovered["state"] == "queued"
        assert recovered["payload"]["status"] == "sealed"
        assert recovered["payload"]["persona"] == snapshot["persona"]
        assert recovered["payload"]["archiveresults"] == snapshot["archiveresults"]

        # Canceling an untouched queue entry is not a saved capture. Discovery
        # must exclude it without fetching its empty manifest for every provider.
        untouched = await archive._request(
            "POST",
            "/api/v1/core/snapshots",
            json={"url": f"https://archivebox.io/tests/cancelled-{uuid4().hex}"},
        )
        untouched_id = untouched["id"]
        try:
            cancelled = await archive._request(
                "PATCH", f"/api/v1/core/snapshot/{untouched_id}", json={"action": "cancel"}
            )
            assert cancelled["status"] == "sealed"
            assert cancelled["downloaded_at"] is None
            assert cancelled["num_archiveresults"] == 0
            assert cancelled["archiveresults"] == []
            store.set_meta("sealed_cursor:" + engine.scope(), cancelled["modified_at"])
            traffic.clear()
            await engine.discover()
            assert not store.has_job(f"saved:{engine.scope()}:{untouched_id}")
            assert all(url.path != f"/api/v1/core/snapshot/{untouched_id}" for url, _, _ in traffic)
            assert store.get(key)["state"] == "queued"
        finally:
            await archive._request("DELETE", f"/api/v1/core/snapshot/{untouched_id}")
    finally:
        for bot in bots:
            await bot.close()
        await archive.close()
        store.close()
