import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def console(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "DATA_DIR": str(tmp_path),
        "PORT": str(port),
        "HOST": "127.0.0.1",
        "ADMIN_PASSWORD": "test-console-password-123",
    }
    proc = subprocess.Popen(
        ["uv", "run", "--project", str(ROOT), "archivebox-slack"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=3)
    try:
        deadline = time.monotonic() + 15
        while True:
            assert proc.poll() is None, proc.stderr.read().decode()
            try:
                if client.get("/healthz").status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            assert time.monotonic() < deadline
            time.sleep(0.1)
        yield client
    finally:
        client.close()
        proc.terminate()
        proc.wait(timeout=10)


def login(client):
    response = client.post("/auth/login", json={"password": "test-console-password-123"})
    assert response.status_code == 200
    client.headers["x-csrf-token"] = response.json()["csrf"]


def test_admin_login_csrf_redaction_and_persisted_settings(console):
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
    assert console.put("/api/settings", json={"enable_ai": True}).status_code == 422
    assert console.put("/api/settings", json={"archivebox_url": "file:///etc/passwd"}).status_code == 422
    assert (
        console.post("/slack/capture/events", json={"type": "url_verification", "challenge": "untrusted"}).status_code
        == 503
    )
    assert console.put("/api/settings", json={"slack_signing_secret": "local-signature-test"}).status_code == 200
    assert (
        console.post("/slack/capture/events", json={"type": "url_verification", "challenge": "untrusted"}).status_code
        == 401
    )
    assert console.get("/api/manifest/capture").json()["settings"]["socket_mode_enabled"] is True
    assert console.get("/api/manifest/ai").json()["features"]["agent_view"]["suggested_prompts"]


def test_missing_zulip_settings_are_actionable_and_password_can_rotate(console):
    login(console)
    assert console.put("/api/settings", json={"platform": "zulip"}).status_code == 200
    response = console.post("/api/check/chat")
    assert response.status_code == 400
    assert "Zulip server URL" in response.json()["detail"]
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
