"""Opt-in real Slack acceptance. Run explicitly with SLACK_TEST_CREDENTIALS set."""

import json
import os
import sqlite3
from pathlib import Path

import pytest

from archivebox_chat_bot.archivebox import ArchiveBox
from archivebox_chat_bot.config import Account, Connection, Settings
from archivebox_chat_bot.slack import Slack


async def test_real_bot_authentication():
    credentials = json.loads(Path(os.environ["SLACK_TEST_CREDENTIALS"]).read_text())["slack"]
    client = Slack(
        Connection(
            id="default",
            platform="slack",
            options={"transport": "http"},
            capture=Account(enabled=True, options={"bot_token": credentials["bot_token"]}),
        )
    )
    try:
        result = await client.check()
        assert result["team_id"] == credentials["team_id"]
        assert result["user_id"] == credentials["user_id"]
    finally:
        await client.close()


@pytest.fixture
async def live():
    """Read back real human UI triggers recorded by the browser acceptance run.

    SLACK_TEST_CREDENTIALS contains slack/slack_ai bot credentials plus slack_test
    (capture_dm, dm_ts, new_channel, channel_ts, saved_channel, thread_root_ts,
    thread_mention_ts, included_urls, excluded_urls) and slack_ai_test
    (dm_channel, dm_ts, mention_channel, mention_ts, stop_ts).
    SLACK_TEST_DATA_DIR points to that running bridge's durable data directory.
    """
    credentials = json.loads(Path(os.environ["SLACK_TEST_CREDENTIALS"]).read_text())
    evidence = {key: credentials[key] for key in ("slack_test", "slack_ai_test")}
    path = Path(os.environ["SLACK_TEST_DATA_DIR"]) / "bridge.sqlite3"
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
        settings = Settings.model_validate_json(db.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
        db.row_factory = sqlite3.Row
        jobs = [dict(row) for row in db.execute("SELECT * FROM jobs").fetchall()]
    for job in jobs:
        for field in ("payload", "result"):
            job[field] = json.loads(job[field])
    capture_connection = next(
        c
        for c in settings.connections
        if c.platform == "slack"
        and c.capture.enabled
        and c.account_options("capture").get("bot_token") == credentials["slack"]["bot_token"]
    )
    ai_connection = next(
        c
        for c in settings.connections
        if c.platform == "slack"
        and c.ai.enabled
        and c.account_options("ai").get("bot_token") == credentials["slack_ai"]["bot_token"]
    )
    capture, ai, archive = Slack(capture_connection), Slack(ai_connection, "ai"), ArchiveBox(settings)
    await capture.check()
    await ai.check()
    try:
        yield evidence, jobs, capture, ai, archive
    finally:
        await capture.close()
        await ai.close()
        await archive.close()


def trigger_job(jobs, ts):
    found = [job for job in jobs if job["kind"] == "message" and job["payload"]["ts"] == ts]
    assert len(found) == 1, "Each human message must create exactly one durable job"
    return found[0]


async def test_real_human_dm_channel_and_thread_captures(live):
    credentials, jobs, capture, _, archive = live
    evidence = credentials["slack_test"]
    all_urls = set()
    for ts, channel, root in (
        (evidence["dm_ts"], evidence["capture_dm"], evidence["dm_ts"]),
        (evidence["channel_ts"], evidence["new_channel"], evidence["channel_ts"]),
        (evidence["thread_mention_ts"], evidence["saved_channel"], evidence["thread_root_ts"]),
    ):
        job = trigger_job(jobs, ts)
        assert job["state"] == "done"
        crawl = await archive.crawl(job["result"]["crawl_id"])
        assert crawl["status"] == "sealed" and crawl["max_depth"] == 0
        urls = set(crawl["urls"].splitlines())
        assert urls == set(job["result"]["urls"])
        all_urls.update(urls)
        snapshots = await archive.crawl_snapshots(crawl["id"])
        assert {snapshot["url"] for snapshot in snapshots} == urls
        sender = await capture.user(job["payload"]["user"])
        for snapshot in snapshots:
            assert snapshot["status"] == "sealed" and snapshot["output_size"] > 0
            assert {"slack", sender["name"]} <= set(snapshot["tags"])
            assert snapshot["persona"] == archive.settings.persona
        reader = capture.client if channel == evidence["capture_dm"] else capture.history
        thread = (await reader.conversations_replies(channel=channel, ts=root, limit=100))["messages"]
        original = next(message for message in thread if message["ts"] == ts)
        reactions = {item["name"] for item in original.get("reactions", [])}
        assert "white_check_mark" in reactions and "runner" not in reactions and "x" not in reactions
        assert not any(message.get("user") == capture.identity["user_id"] for message in thread)
    assert all_urls == set(evidence["included_urls"])
    assert not all_urls.intersection(evidence["excluded_urls"])


async def test_real_ai_replies_and_cancelled_session(live):
    credentials, jobs, _, ai, archive = live
    evidence = credentials["slack_ai_test"]
    session_ids = []
    for kind in ("dm", "mention"):
        ts = evidence[f"{kind}_ts"]
        job = trigger_job(jobs, ts)
        assert job["state"] == "done"
        session = job["result"]["session"]
        session_ids.append(session["id"])
        answer = await archive.session_answer(session)
        assert answer is not None and answer == job["result"]["answer"]
        reader = ai.client if kind == "dm" else ai.history
        thread = (await reader.conversations_replies(channel=evidence[f"{kind}_channel"], ts=ts))["messages"]
        replies = [message for message in thread if message.get("user") == ai.identity["user_id"]]
        assert len(replies) == 1 and replies[0]["text"] == answer
    assert len(set(session_ids)) == 2
    stopped_ts = evidence.get("stop_fixed_ts", evidence["stop_ts"])
    stopped = trigger_job(jobs, stopped_ts)
    assert stopped["state"] == "cancelled"
    thread = (await ai.client.conversations_replies(channel=evidence["dm_channel"], ts=stopped_ts))["messages"]
    reactions = {item["name"] for item in thread[0].get("reactions", [])}
    assert "x" in reactions and "runner" not in reactions
    assert not any(message.get("user") == ai.identity["user_id"] for message in thread)
    statuses = await archive._agent_request("GET", "session/status")
    status = statuses.get(stopped["result"]["session"]["id"], {"type": "idle"})
    assert status["type"] == "idle"


async def test_real_saved_cards_are_single_line_and_processed_images(live):
    credentials, jobs, capture, _, archive = live
    evidence = credentials["slack_test"]
    crawl = trigger_job(jobs, evidence["dm_ts"])["result"]["crawl_id"]
    for snapshot in await archive.crawl_snapshots(crawl):
        cards = [job for job in jobs if job["kind"] == "announcement" and job["payload"]["id"] == snapshot["id"]]
        assert len(cards) == 1 and cards[0]["state"] == "done"
        messages = (
            await capture.history.conversations_history(
                channel=evidence["saved_channel"], oldest=cards[0]["result"]["message_id"], inclusive=True, limit=1
            )
        )["messages"]
        card = messages[0]
        assert card["ts"] == cards[0]["result"]["message_id"]
        assert "\n" not in card["text"]
        assert snapshot["url"] in card["text"] and "👤" in card["text"]
        assert len(card["blocks"]) == 1 and card["blocks"][0]["type"] == "section"
        image = card["blocks"][0]["accessory"]["slack_file"]["id"]
        file = (await capture.client.files_info(file=image))["file"]
        assert file["filetype"] == "png" and file["original_w"] > 0
