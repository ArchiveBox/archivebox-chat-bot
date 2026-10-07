from archivebox_slack.store import Store
from archivebox_slack.text import extract_urls, submitter_tag


def test_slack_markdown_and_balanced_url_extraction():
    assert extract_urls(
        "Read <https://example.com/a?x=1&amp;y=2|this> and [wiki](https://en.wikipedia.org/wiki/Archive_(disambiguation)). https://example.com/a?x=1&y=2"
    ) == ["https://example.com/a?x=1&y=2", "https://en.wikipedia.org/wiki/Archive_(disambiguation)"]
    assert (
        extract_urls("https://user:password@example.com ftp://example.com javascript:alert(1) https://broken:port/")
        == []
    )
    assert submitter_tag("Nick,\nSmith", "U123") == "Nick  Smith"


def test_durable_event_deduplication_and_ambiguous_restart(tmp_path):
    store = Store(tmp_path)
    assert store.enqueue("slack:capture:C:1", "message", {"text": "https://example.com"})
    assert not store.enqueue("slack:capture:C:1", "message", {"text": "https://example.com"})
    store.update("slack:capture:C:1", "running", {"session": {"id": "persisted-session"}})
    store.close()
    reopened = Store(tmp_path)
    assert reopened.get("slack:capture:C:1")["state"] == "uncertain"
    assert reopened.get("slack:capture:C:1")["result"]["session"]["id"] == "persisted-session"
    assert not reopened.jobs("queued")
    reopened.close()


def test_durable_jobs_stay_bound_to_their_original_connection(tmp_path):
    store = Store(tmp_path)
    store.enqueue("source-a:event", "message", {"text": "private original"}, scope="source-a")
    store.enqueue("source-b:event", "message", {"text": "private replacement"}, scope="source-b")
    first = store.jobs("queued", scope="source-a")
    second = store.jobs("queued", scope="source-b")
    assert [row["payload"]["text"] for row in first] == ["private original"]
    assert [row["payload"]["text"] for row in second] == ["private replacement"]
    store.close()
