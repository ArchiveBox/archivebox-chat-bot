import pytest


def login(client):
    response = client.post("/auth/login", json={"password": "test-console-password-123"})
    assert response.status_code == 200
    client.headers["x-csrf-token"] = response.json()["csrf"]


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
