import os
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def console(tmp_path, request):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "DATA_DIR": str(tmp_path),
        "PORT": str(port),
        "HOST": "127.0.0.1",
        "ADMIN_PASSWORD": "" if getattr(request, "param", "") == "first-run" else "test-console-password-123",
    }
    proc = subprocess.Popen(
        ["uv", "run", "--project", str(ROOT), "archivebox-chat-bot"],
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
