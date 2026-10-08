"""Acceptance tests against a running, disposable real ArchiveBox collection.

Set ARCHIVEBOX_TEST_URL and ARCHIVEBOX_TEST_TOKEN_FILE before running this file.
The collection must have the title, screenshot, favicon, wget and opencode plugins
installed, with OpenCode enabled. No ArchiveBox responses are substituted.
"""

import asyncio
import os
from pathlib import Path
from time import monotonic
from uuid import uuid4

import pytest

from archivebox_chat_bot.archivebox import ArchiveBox
from archivebox_chat_bot.config import Settings


@pytest.fixture
async def archivebox():
    url = os.environ["ARCHIVEBOX_TEST_URL"]
    token = Path(os.environ["ARCHIVEBOX_TEST_TOKEN_FILE"]).read_text().strip()
    client = ArchiveBox(
        Settings(
            archivebox_url=url,
            archivebox_admin_url=os.environ.get("ARCHIVEBOX_TEST_ADMIN_URL", ""),
            archivebox_public_url=url,
            archivebox_token=token,
            plugins="title,screenshot,favicon,wget",
        )
    )
    try:
        yield client
    finally:
        await client.close()


async def test_real_screenshot_download(archivebox):
    """A sealed capture's PNG must download through ArchiveBox's real routing."""
    page = await archivebox.snapshots(status="sealed", with_archiveresults=True, limit=100)
    snapshots = [
        row
        for row in page["items"]
        if any(r["plugin"] == "screenshot" and r["status"] == "succeeded" for r in row["archiveresults"])
    ]
    assert snapshots, "Capture a screenshot in the disposable collection first"
    image = await archivebox.artifact(snapshots[0], "screenshot")
    assert image is not None
    data, mimetype, url = image
    assert data.startswith(b"\x89PNG\r\n\x1a\n")
    assert mimetype == "image/png"
    response = await archivebox._browser.get(url, headers=await archivebox._browser_headers())
    assert response.status_code == 200 and response.content == data


async def test_real_server_base_url_discovers_api_host():
    url = os.environ.get("ARCHIVEBOX_TEST_BASE_URL", os.environ["ARCHIVEBOX_TEST_URL"])
    token_file = os.environ.get("ARCHIVEBOX_TEST_BASE_TOKEN_FILE", os.environ["ARCHIVEBOX_TEST_TOKEN_FILE"])
    client = ArchiveBox(Settings(archivebox_url=url, archivebox_token=Path(token_file).read_text().strip()))
    try:
        assert (await client.check())["ok"]
        session = await client.create_session("Slack server base URL acceptance " + uuid4().hex)
        assert session["id"] and session["directory"]
    finally:
        await client.close()


async def test_real_search_excludes_configuration_only_matches(archivebox):
    matches = await archivebox.search("archivebox.io")
    assert matches
    assert all("archivebox.io" in " ".join([row["url"], row["title"], *row["tags"]]).casefold() for row in matches)
    titles = await archivebox.search("Example Domain")
    assert titles and all("example domain" in row["title"].casefold() for row in titles)
    assert await archivebox.search(uuid4().hex) == []


async def test_real_crawl_metadata_and_images(archivebox):
    check = await archivebox.check()
    assert check["ok"] and isinstance(check["snapshots"], int)
    target = os.environ.get("ARCHIVEBOX_TEST_CAPTURE_URL", "https://archivebox.io/")
    separator = "&" if "?" in target else "?"
    target += f"{separator}slack-acceptance={uuid4().hex}"
    queued = await archivebox.add([target], "ArchiveBox Slack Acceptance")
    deadline = monotonic() + 180
    while True:
        crawl = await archivebox.crawl(queued["crawl_id"])
        if crawl["status"] == "sealed":
            break
        assert monotonic() < deadline, f"Crawl did not seal: {crawl['status']}"
        await asyncio.sleep(1)
    assert crawl["max_depth"] == 0
    assert crawl["urls"] == target
    rows = await archivebox.crawl_snapshots(queued["crawl_id"])
    assert len(rows) == 1
    snapshot = rows[0]
    assert snapshot["url"] == target
    assert snapshot["status"] == "sealed"
    assert snapshot["persona"] == "Default"
    assert {"slack", "ArchiveBox Slack Acceptance"} <= set(snapshot["tags"])
    assert snapshot["title"]
    assert snapshot["output_size"] > 0
    assert archivebox.detail_url(snapshot).endswith(snapshot["archive_path"])
    for kind in ("screenshot", "favicon"):
        image = await archivebox.artifact(snapshot, kind)
        assert image is not None, f"The real {kind} extractor must produce an image"
        assert image[0] and image[1].startswith("image/")
    matches = await archivebox.search(target)
    assert any(row["id"] == snapshot["id"] for row in matches)


async def test_real_opencode_session_is_persisted(archivebox):
    title = "Slack live integration " + uuid4().hex
    session = await archivebox.create_session(title)
    assert session["id"].startswith("ses_")
    assert session["title"] == title
    assert session["directory"]
    page = await archivebox._browser.get(archivebox.session_url(session), headers=await archivebox._browser_headers())
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "<script" in page.text
    sessions = await archivebox._agent_request(
        "GET", "session", params={"directory": session["directory"], "roots": "true", "limit": 100}
    )
    assert any(row["id"] == session["id"] and row["title"] == title for row in sessions)
    prompt = "Say hello briefly. Do not use any tools."
    answer = await archivebox.prompt_session(session, prompt)
    assert answer.strip()
    messages = await archivebox._agent_request(
        "GET", f"session/{session['id']}/message", params={"directory": session["directory"]}
    )
    assert any(
        row["info"]["role"] == "user" and any(part.get("text") == prompt for part in row["parts"]) for row in messages
    )
    assert any(
        row["info"]["role"] == "assistant"
        and "\n\n".join(part["text"] for part in row["parts"] if part.get("type") == "text" and part.get("text"))
        == answer
        for row in messages
    )


async def test_repeated_human_submissions_create_tagged_captures(archivebox):
    target = f"https://example.com/?slack-repeated-submission={uuid4().hex}"
    first = await archivebox.add([target], "First Slack Submitter")
    first_deadline = monotonic() + 180
    while (await archivebox.crawl(first["crawl_id"]))["status"] != "sealed":
        assert monotonic() < first_deadline, "First submission did not seal"
        await asyncio.sleep(1)
    second = await archivebox.add([target], "Second Slack Submitter")
    assert second["crawl_id"] != first["crawl_id"]
    second_deadline = monotonic() + 180
    while (await archivebox.crawl(second["crawl_id"]))["status"] != "sealed":
        assert monotonic() < second_deadline, "Second submission did not seal"
        await asyncio.sleep(1)
    first_rows = await archivebox.crawl_snapshots(first["crawl_id"])
    second_rows = await archivebox.crawl_snapshots(second["crawl_id"])
    assert len(first_rows) == len(second_rows) == 1
    assert first_rows[0]["id"] != second_rows[0]["id"]
    assert first_rows[0]["url"] == second_rows[0]["url"] == target
    assert {"slack", "First Slack Submitter"} <= set(first_rows[0]["tags"])
    assert {"slack", "Second Slack Submitter"} <= set(second_rows[0]["tags"])
    assert "First Slack Submitter" not in second_rows[0]["tags"]
    assert "Second Slack Submitter" not in first_rows[0]["tags"]
    assert first_rows[0]["output_size"] > 0 and second_rows[0]["output_size"] > 0
