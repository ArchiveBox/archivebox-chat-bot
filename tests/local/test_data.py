from archivebox_chat_bot.store import Store
from archivebox_chat_bot.text import extract_urls, submission_tags, submitter_tag


def test_submission_tags_are_separate_and_preserve_names():
    assert submission_tags("slack", "bob", "#accounting") == ["slack", "bob", "accounting"]
    assert submission_tags("telegram", "Bob Smith", "Team Finance") == ["telegram", "Bob Smith", "Team Finance"]
    assert submission_tags("zulip", "Zoë", "Comptabilité") == ["zulip", "Zoë", "Comptabilité"]
    assert submission_tags("irc", "bob") == ["irc", "bob"]
    assert submission_tags("slack", "Bob, Jr.", "accounting\n,finance") == ["slack", "Bob  Jr.", "accounting  finance"]
    assert submission_tags("slack", "slack", "") == ["slack"]


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


def test_real_archived_svg_favicon_renders_as_png():
    import io
    from pathlib import Path

    from PIL import Image

    from archivebox_chat_bot.media import normalize_image

    favicon = Path(__file__).parents[1] / "fixtures/mdn-favicon.svg"
    result = normalize_image(favicon.read_bytes(), "favicon")
    with Image.open(io.BytesIO(result)) as image:
        assert image.format == "PNG"
        assert image.size == (32, 32)
        assert image.getbbox() is not None
