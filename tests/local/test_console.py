import pytest

from archivebox_chat_bot.config import PLATFORMS


def login(client):
    response = client.post("/auth/login", json={"password": "test-console-password-123"})
    assert response.status_code == 200
    client.headers["x-csrf-token"] = response.json()["csrf"]


def test_activity_records_auth_setup_and_parse_errors_without_secrets(console):
    assert console.get("/api/events").status_code == 401
    assert console.post("/auth/login", json={"password": "private-wrong-password"}).status_code == 401
    login(console)
    response = console.put("/api/settings", content='{"archivebox_token":"private-broken-json"')
    assert response.status_code == 400
    assert (
        console.put("/api/settings", json={"connections": [{"id": "incomplete", "platform": "slack"}]}).status_code
        == 200
    )
    assert console.post("/api/check/chat", params={"connection_id": "absent"}).status_code == 400
    response = console.get("/api/events")
    assert response.status_code == 200
    events = response.json()["events"]
    assert any(e["status_code"] == 401 and "Incorrect password" in e["message"] for e in events)
    assert any(e["status_code"] == 400 and "JSON" in e["message"] for e in events)
    assert any(e["kind"] == "connection" and "Connecting" in e["message"] for e in events)
    assert any(e["level"] == "error" and "xoxb-" in e["message"] for e in events)
    assert any(e["kind"] == "settings" for e in events)
    assert all(e["elapsed_ms"] is None or e["elapsed_ms"] >= 0 for e in events)
    assert "private-wrong-password" not in response.text and "private-broken-json" not in response.text


def test_multiple_connections_of_every_provider_persist_and_edit_independently(console):
    login(console)
    connections = [
        {"id": f"{platform}-{number}", "platform": platform, "name": f"{platform} {number}", "enabled": False}
        for platform in PLATFORMS
        for number in (1, 2)
    ]
    assert console.put("/api/settings", json={"connections": connections}).status_code == 200
    saved = console.get("/api/state").json()["settings"]["connections"]
    assert {c["id"] for c in saved} == {c["id"] for c in connections}
    saved[0]["name"] = "Changed independently"
    assert console.put("/api/settings", json={"connections": saved}).status_code == 200
    actual = console.get("/api/state").json()["settings"]["connections"]
    assert actual[0]["name"] == "Changed independently"
    assert actual[1:] == saved[1:]


def test_admin_login_csrf_redaction_and_persisted_settings(console):
    assert console.get("/auth/setup").json() == {"required": False}
    assert console.post("/auth/setup", json={"password": "replacement-password"}).status_code == 409
    assert console.get("/").status_code == 200
    assert console.get("/api/state").status_code == 401
    assert console.post("/auth/login", json={"password": "incorrect"}).status_code == 401
    login(console)
    csrf = console.headers.pop("x-csrf-token")
    assert console.put("/api/settings", json={"persona": "Research"}).status_code == 403
    console.headers["x-csrf-token"] = csrf
    assert (
        console.put(
            "/api/settings", headers={"Origin": "https://attacker.invalid"}, json={"persona": "Research"}
        ).status_code
        == 403
    )
    assert (
        console.put(
            "/api/settings", json={"persona": "Research", "archivebox_token": "stored-secret-value"}
        ).status_code
        == 200
    )
    state = console.get("/api/state").json()
    assert state["settings"]["persona"] == "Research"
    assert state["settings"]["archivebox_token"] == ""
    assert "archivebox_token" in state["settings"]["configured_secrets"]
    assert "stored-secret-value" not in str(state)
    assert console.put("/api/settings", json={"archivebox_token": ""}).status_code == 200
    assert "archivebox_token" in console.get("/api/state").json()["settings"]["configured_secrets"]
    assert console.put("/api/settings", json={"clear_secrets": ["archivebox_token"]}).status_code == 200
    assert "archivebox_token" not in console.get("/api/state").json()["settings"]["configured_secrets"]
    assert console.post("/auth/logout").status_code == 200
    assert console.get("/api/state").status_code == 401


def test_preferences_and_untrusted_event_security(console):
    login(console)
    assert (
        console.put("/api/settings", json={"connections": [{"id": "bad", "platform": "unsupported"}]}).status_code
        == 422
    )
    assert console.put("/api/settings", json={"archivebox_url": "file:///etc/passwd"}).status_code == 422
    assert (
        console.put(
            "/api/settings", json={"connections": [{"id": "default", "platform": "slack", "enabled": False}]}
        ).status_code
        == 200
    )
    assert (
        console.post("/slack/capture/events", json={"type": "url_verification", "challenge": "untrusted"}).status_code
        == 503
    )
    assert (
        console.put(
            "/api/settings",
            json={
                "connections": [
                    {
                        "id": "default",
                        "platform": "slack",
                        "enabled": False,
                        "capture": {"options": {"signing_secret": "local-signature-test"}},
                    }
                ]
            },
        ).status_code
        == 200
    )
    assert (
        console.post("/slack/capture/events", json={"type": "url_verification", "challenge": "untrusted"}).status_code
        == 401
    )
    assert console.get("/api/manifest/capture").json()["settings"]["socket_mode_enabled"] is True
    assert console.get("/api/manifest/ai").json()["features"]["agent_view"]["suggested_prompts"]


def test_missing_zulip_settings_are_actionable_and_password_can_rotate(console):
    login(console)
    assert (
        console.put("/api/settings", json={"connections": [{"id": "default", "platform": "zulip"}]}).status_code == 200
    )
    response = console.post("/api/check/chat")
    assert response.status_code == 400
    assert "connect this bot" in response.json()["detail"]
    assert (
        "Zulip server URL"
        in console.get("/api/state").json()["connections"]["chat"]["default"]["roles"]["capture"]["error"]
    )
    assert (
        console.post(
            "/api/password", json={"current_password": "wrong", "new_password": "new-test-password-123"}
        ).status_code
        == 403
    )
    assert (
        console.post(
            "/api/password",
            json={"current_password": "test-console-password-123", "new_password": "new-test-password-123"},
        ).status_code
        == 200
    )
    assert console.post("/auth/logout").status_code == 200
    assert console.post("/auth/login", json={"password": "test-console-password-123"}).status_code == 401
    assert console.post("/auth/login", json={"password": "new-test-password-123"}).status_code == 200


def test_slack_setup_explains_missing_tokens(console):
    login(console)
    connection = {"id": "slack-setup", "platform": "slack"}
    assert console.put("/api/settings", json={"connections": [connection]}).status_code == 200
    state = console.get("/api/state").json()
    error = state["connections"]["chat"]["slack-setup"]["roles"]["capture"]["error"]
    assert "Bot User OAuth Token" in error and "xoxb-" in error
    connection["capture"] = {"enabled": True, "options": {"bot_token": "xoxb-local-format-check"}}
    assert console.put("/api/settings", json={"connections": [connection]}).status_code == 200
    state = console.get("/api/state").json()
    error = state["connections"]["chat"]["slack-setup"]["roles"]["capture"]["error"]
    assert "App-Level Token" in error and "xapp-" in error
    assert not state["settings"]["connections"][0]["new_channel"]
    assert not state["settings"]["connections"][0]["saved_channel"]


@pytest.mark.parametrize("console", ["first-run"], indirect=True)
def test_first_run_password_setup_is_one_time_and_persisted(console, tmp_path):
    import sqlite3

    assert console.get("/auth/setup").json() == {"required": True}
    assert console.get("/api/state").status_code == 401
    assert console.post("/auth/setup", json={"password": "short"}).status_code == 422
    assert (
        console.post(
            "/auth/setup",
            headers={"Origin": "https://attacker.invalid"},
            json={"password": "test-console-password-123"},
        ).status_code
        == 403
    )
    assert (
        console.post(
            "/auth/setup", headers={"Content-Type": "text/plain"}, content='{"password":"test-console-password-123"}'
        ).status_code
        == 415
    )
    result = console.post("/auth/setup", json={"password": "test-console-password-123"})
    assert result.status_code == 200
    assert console.get("/auth/setup").json() == {"required": False}
    assert console.get("/api/state").status_code == 200
    assert not (tmp_path / "admin-password").exists()
    with sqlite3.connect(tmp_path / "bridge.sqlite3") as db:
        digest = db.execute("SELECT value FROM meta WHERE key='password_hash'").fetchone()[0]
        assert len(digest) == 128 and digest != "test-console-password-123"
    console.headers["x-csrf-token"] = result.json()["csrf"]
    assert console.post("/auth/logout").status_code == 200
    assert console.post("/auth/setup", json={"password": "replacement-password"}).status_code == 409
    login(console)
    assert console.get("/api/state").status_code == 200


def test_discord_setup_requires_token_and_valid_role(console):
    login = console.post("/auth/login", json={"password": "test-console-password-123"})
    assert login.status_code == 200
    console.headers["x-csrf-token"] = login.json()["csrf"]
    missing = console.post("/api/discord/discover", json={"role": "capture", "options": {}})
    assert missing.status_code == 400
    assert "bot token" in missing.json()["detail"]
    invalid = console.post("/api/discord/discover", json={"role": "unknown"})
    assert invalid.status_code == 422
    state = console.get("/api/state").json()
    assert state["settings"]["connections"] == []


def test_email_is_inbound_only_and_keeps_password_private(console):
    login(console)
    connection = {
        "id": "email",
        "platform": "email",
        "enabled": False,
        "capture": {
            "enabled": True,
            "options": {
                "host": "imap.example.org",
                "username": "archive@example.org",
                "password": "private-app-password",
            },
        },
        "allowed_users": ["Alice@Example.org"],
    }
    response = console.put("/api/settings", json={"connections": [connection]})
    assert response.status_code == 200
    actual = response.json()["settings"]["connections"][0]
    assert actual["enable_saved_urls"] is False and actual["enable_mentions"] is False
    assert actual["commands"] == [] and actual["allowed_users"] == ["alice@example.org"]
    assert actual["capture"]["options"]["password"] == ""
    assert "private-app-password" not in console.get("/api/state").text
    connection["ai"] = {"enabled": True}
    rejected = console.put("/api/settings", json={"connections": [connection]})
    assert rejected.status_code == 422 and "inbound-only" in rejected.text


def test_connection_dashboard_scopes_activity_and_exports_csv(console, tmp_path):
    import csv
    import io
    import json
    import sqlite3

    login(console)
    assert (
        console.put(
            "/api/settings",
            json={
                "connections": [
                    {"id": "one", "platform": "telegram", "enabled": False},
                    {"id": "two", "platform": "telegram", "enabled": False},
                ]
            },
        ).status_code
        == 200
    )
    with sqlite3.connect(tmp_path / "bridge.sqlite3") as db:
        for key in ("one", "two"):
            db.execute(
                "INSERT INTO jobs(id,kind,payload,state,result,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                (
                    key,
                    "message",
                    json.dumps(
                        {
                            "connection": key,
                            "role": "capture",
                            "user": "u",
                            "channel": "dm",
                            "text": "https://example.org/" + key,
                        }
                    ),
                    "done",
                    json.dumps({"urls": ["https://example.org/" + key]}),
                    "2026-10-08T00:00:00+00:00",
                    "2026-10-08T00:00:00+00:00",
                ),
            )
            db.execute(
                "INSERT INTO events(created_at,level,kind,connection,role,message) VALUES(?,?,?,?,?,?)",
                ("2026-10-08T00:00:00+00:00", "info", "message", key, "capture", "=private-" + key),
            )
    state = console.get("/api/state").json()
    first = state["connection_activity"]["one"]
    assert first["urls_saved"] == 1 and first["last_message_at"] == "2026-10-08T00:00:00+00:00"
    assert all(row["connection"] == "one" for row in first["events"])
    response = console.get("/api/connections/one/export.csv")
    assert response.status_code == 200 and "text/csv" in response.headers["content-type"]
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert any(row["url"] == "https://example.org/one" for row in rows)
    assert any(row["message"] == "'=private-one" for row in rows)
    assert "private-two" not in response.text and "https://example.org/two" not in response.text
    assert console.get("/api/connections/absent/export.csv").status_code == 404
    assert console.post("/api/connections/one/test", json={"role": "capture", "user": "stranger"}).status_code == 400
