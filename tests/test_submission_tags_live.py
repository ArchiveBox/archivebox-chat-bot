"""Verify tags on real human chat captures recorded by browser acceptance.

CHAT_TAG_EVIDENCE selects the protected JSON evidence file; CHAT_TAG_CASES is a
comma-separated list of its keys. CHAT_TAG_DATA_DIR is the running bot's data.
The evidence records provider, sender_name, channel_name (empty for DMs),
message_ts, and url. No messages or API responses are fabricated here.
"""

import json
import os
import sqlite3
from pathlib import Path

from archivebox_chat_bot.archivebox import ArchiveBox
from archivebox_chat_bot.config import Settings


async def test_human_submissions_have_separate_source_sender_and_group_tags():
    evidence = json.loads(Path(os.environ["CHAT_TAG_EVIDENCE"]).read_text())
    path = Path(os.environ["CHAT_TAG_DATA_DIR"]) / "bridge.sqlite3"
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        settings = Settings.model_validate_json(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
        jobs = [
            (json.loads(payload), json.loads(result), state)
            for payload, result, state in db.execute("SELECT payload,result,state FROM jobs WHERE kind='message'")
        ]
    archive = ArchiveBox(settings)
    try:
        for key in os.environ["CHAT_TAG_CASES"].split(","):
            case = evidence[key]
            found = [(p, r, s) for p, r, s in jobs if p["ts"] == case["message_ts"]]
            assert len(found) == 1, f"Expected one durable job for the real {key} message"
            payload, result, state = found[0]
            assert state == "done", (key, state)
            crawl = await archive.crawl(result["crawl_id"])
            assert crawl["status"] == "sealed" and crawl["max_depth"] == 0
            expected = {case["provider"], case["sender_name"]}
            if case.get("channel_name"):
                expected.add(case["channel_name"])
            snapshots = await archive.crawl_snapshots(result["crawl_id"])
            assert any(row["url"] == case["url"] for row in snapshots)
            for snapshot in snapshots:
                assert snapshot["status"] == "sealed"
                assert set(snapshot["tags"]) == expected, (key, snapshot["url"], snapshot["tags"], expected)
                assert payload["user"] == case["sender_id"]
    finally:
        await archive.close()
