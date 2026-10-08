import hashlib
import os
import socket
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]


@contextmanager
def chatbot_server(data_dir):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "DATA_DIR": str(data_dir),
        "PORT": str(port),
        "HOST": "127.0.0.1",
        "ADMIN_PASSWORD": "",
    }
    process = subprocess.Popen(
        ["uv", "run", "--project", str(ROOT), "archivebox-chat-bot"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 15
        while True:
            if process.poll() is not None:
                raise AssertionError(process.stderr.read().decode())
            try:
                if httpx.get(f"{base_url}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.ConnectError:
                pass
            assert time.monotonic() < deadline, "Chatbot server did not start"
            time.sleep(0.1)
        yield base_url
    finally:
        process.terminate()
        process.wait(timeout=10)


def setup_password(client, password):
    response = client.post("/auth/setup", json={"password": password})
    assert response.status_code == 200
    return response.json()["csrf"], response.cookies["abx_chat_session"]


def sign_in(client, password):
    response = client.post("/auth/login", json={"password": password})
    assert response.status_code == 200
    return response.json()["csrf"], response.cookies["abx_chat_session"]


def test_sessions_survive_process_restart_and_revoke_on_logout_or_password_change(tmp_path):
    password = "test-persistent-session-password"
    with (
        chatbot_server(tmp_path) as base_url,
        httpx.Client(base_url=base_url) as first,
        httpx.Client(base_url=base_url) as second,
    ):
        csrf, token = setup_password(first, password)
        assert first.get("/api/state").status_code == 200
        other_csrf, other_token = sign_in(second, password)
        assert other_token != token
        with sqlite3.connect(tmp_path / "bridge.sqlite3") as db:
            tokens = {row[0] for row in db.execute("SELECT token_hash FROM admin_sessions")}
        assert hashlib.sha256(token.encode()).hexdigest() in tokens
        assert token not in tokens

    with chatbot_server(tmp_path) as base_url:
        with httpx.Client(base_url=base_url, cookies={"abx_chat_session": token}) as first:
            first.headers["x-csrf-token"] = csrf
            assert first.get("/api/state").status_code == 200
            first.headers.pop("x-csrf-token")
            assert first.put("/api/settings", json={"persona": "Persistence check"}).status_code == 403
            first.headers["x-csrf-token"] = csrf
            assert first.put("/api/settings", json={"persona": "Persistence check"}).status_code == 200

        with httpx.Client(base_url=base_url, cookies={"abx_chat_session": other_token}) as second:
            second.headers["x-csrf-token"] = other_csrf
            assert (
                second.post(
                    "/api/password",
                    json={"current_password": password, "new_password": "rotated-persistent-password"},
                ).status_code
                == 200
            )

        with httpx.Client(base_url=base_url, cookies={"abx_chat_session": token}) as first:
            assert first.get("/api/state").status_code == 401

        with httpx.Client(base_url=base_url, cookies={"abx_chat_session": other_token}) as second:
            second.headers["x-csrf-token"] = other_csrf
            assert second.get("/api/state").status_code == 200
            assert second.post("/auth/logout").status_code == 200
            assert second.get("/api/state").status_code == 401


def test_expired_session_is_rejected_after_process_restart(tmp_path):
    with chatbot_server(tmp_path) as base_url, httpx.Client(base_url=base_url) as client:
        _, token = setup_password(client, "test-persistent-session-password")

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with sqlite3.connect(tmp_path / "bridge.sqlite3") as db:
        db.execute("UPDATE admin_sessions SET expires=? WHERE token_hash=?", (time.time() - 1, token_hash))

    with (
        chatbot_server(tmp_path) as base_url,
        httpx.Client(base_url=base_url, cookies={"abx_chat_session": token}) as client,
    ):
        assert client.get("/api/state").status_code == 401
        with sqlite3.connect(tmp_path / "bridge.sqlite3") as db:
            assert db.execute("SELECT 1 FROM admin_sessions WHERE token_hash=?", (token_hash,)).fetchone() is None
