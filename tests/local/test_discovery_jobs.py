import sqlite3

from archivebox_chat_bot.store import Store


def test_discovery_identity_lookup_never_reads_capture_payload(tmp_path):
    store = Store(tmp_path)
    try:
        assert not store.has_job("saved:scope-a:capture")
        payload = {"archiveresults": [{"output_files": {"title.txt": {"size": 14}}, "stdout": "a" * 100_000}]}
        for state in ("queued", "uncertain", "done"):
            key = f"saved:{state}:capture"
            assert store.enqueue(key, "announcement", payload, scope=state)
            store.update(key, state)

        def forbid_payload_read(action, table, column, _database, _trigger):
            # SQLite itself rejects payload reads, including SELECT *. A cheap
            # duplicate check must not materialize the saved manifests.
            if action == sqlite3.SQLITE_READ and table == "jobs" and column in {"payload", "result"}:
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        store.db.set_authorizer(forbid_payload_read)
        for state in ("queued", "uncertain", "done"):
            assert store.has_job(f"saved:{state}:capture")
        assert not store.has_job("saved:another-scope:capture")
        store.db.set_authorizer(None)
        assert all(job["payload"] == payload for job in store.jobs())
    finally:
        store.close()
