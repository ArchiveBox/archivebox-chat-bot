from archivebox_chat_bot.models import Message
from archivebox_chat_bot.store import Store


def message(role="capture", ts="previous", text="private previous source"):
    return Message("irc", role, "#research", "account:researcher", text, ts)


def test_replacing_authenticated_source_isolates_history_and_group_policy(tmp_path):
    store = Store(tmp_path)
    store.bind_identity("irc", "capture", "server-a:6697/account:bot")
    store.remember("irc", message())
    store.set_group("irc", "#research", auto_archive=True)
    assert store.recent("irc", message(ts="next")) == ["private previous source"]
    assert store.group_policy("irc", "#research")

    store.bind_identity("irc", "capture", "server-b:6697/account:bot")
    assert store.recent("irc", message(ts="next")) == []
    assert store.groups("irc") == []
    assert not store.group_policy("irc", "#research")
    # The old message ID can legitimately occur on an unrelated server.
    store.remember("irc", message(text="new source"))
    assert store.recent("irc", message(ts="next")) == ["new source"]
    recovery = store.db.execute("SELECT namespace FROM identity_quarantine").fetchone()[0]
    assert (
        store.db.execute("SELECT text FROM history WHERE connection=?", (recovery,)).fetchone()[0]
        == "private previous source"
    )
    assert store.db.execute("SELECT auto_archive FROM groups WHERE connection=?", (recovery,)).fetchone()[0] == 1
    store.close()


def test_stable_identity_survives_restart_without_resetting_policy(tmp_path):
    store = Store(tmp_path)
    store.bind_identity("irc", "capture", "server-a/account:bot")
    store.remember("irc", message())
    store.set_group("irc", "#research", auto_archive=True)
    store.close()
    store = Store(tmp_path)
    store.bind_identity("irc", "capture", "server-a/account:bot")
    assert store.recent("irc", message(ts="next")) == ["private previous source"]
    assert store.group_policy("irc", "#research")
    assert store.db.execute("SELECT count(*) FROM identity_quarantine").fetchone()[0] == 0
    store.close()


def test_capture_and_ai_bindings_do_not_invalidate_each_other(tmp_path):
    store = Store(tmp_path)
    store.bind_identity("irc", "capture", "server-a/account:capture")
    store.remember("irc", message())
    store.set_group("irc", "#research", auto_archive=True)
    store.bind_identity("irc", "ai", "server-a/account:ai")
    store.remember("irc", message(role="ai", text="private AI context"))
    for role in ("capture", "ai", "capture", "ai"):
        store.bind_identity("irc", role, "server-a/account:" + role)
    assert store.group_policy("irc", "#research")
    assert store.recent("irc", message(role="ai", ts="next")) == ["private AI context"]
    store.bind_identity("irc", "ai", "server-a/account:replacement")
    assert store.recent("irc", message(role="ai", ts="next")) == []
    assert store.recent("irc", message(ts="next")) == ["private previous source"]
    assert not store.group_policy("irc", "#research")
    store.set_group("irc", "#research", auto_archive=True)
    store.bind_identity("irc", "capture", "server-a/account:capture")
    store.bind_identity("irc", "ai", "server-a/account:replacement")
    assert store.group_policy("irc", "#research")
    store.close()


def test_legacy_unbound_rows_are_preserved_but_not_trusted(tmp_path):
    store = Store(tmp_path)
    store.remember("irc", message())
    store.remember("irc", message(role="ai", text="legacy AI context"))
    store.set_group("irc", "#research", auto_archive=True)
    store.close()
    store = Store(tmp_path)
    store.bind_identity("irc", "capture", "verified/account:capture")
    assert not store.group_policy("irc", "#research")
    assert store.recent("irc", message(ts="next")) == []
    store.set_group("irc", "#research", auto_archive=True)
    store.bind_identity("irc", "ai", "verified/account:ai")
    assert store.recent("irc", message(role="ai", ts="next")) == []
    assert store.group_policy("irc", "#research")
    assert store.db.execute("SELECT count(*) FROM history WHERE connection!='irc'").fetchone()[0] == 2
    store.close()
