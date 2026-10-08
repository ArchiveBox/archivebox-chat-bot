"""Real inbound IMAP acceptance against the official Dovecot Docker image.

Run: uv run pytest tests/test_email_live.py -q
Docker and openssl are required. Mail and certificates use temporary bind mounts;
messages enter through IMAP APPEND, and no SMTP listener is published.
"""

import asyncio
import imaplib
import json
import os
import shutil
import ssl
import subprocess
import tempfile
import time
import uuid
from email.message import EmailMessage
from pathlib import Path

import pytest

from archivebox_chat_bot.config import Connection, Settings
from archivebox_chat_bot.connectors import TransportBot
from archivebox_chat_bot.engine import Engine
from archivebox_chat_bot.store import Store
from archivebox_chat_bot.text import extract_urls


@pytest.fixture(scope="module")
def imap_server():
    # Docker Desktop shares Downloads on this host; keep all mail outside source checkouts.
    scratch = Path.home() / "Downloads" if os.uname().sysname == "Darwin" else Path(tempfile.gettempdir())
    root = Path(tempfile.mkdtemp(prefix="email-dovecot-", dir=scratch))
    mail = root / "mail"
    certs = root / "certs"
    mail.mkdir(mode=0o777)
    mail.chmod(0o777)
    certs.mkdir()
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-keyout",
            str(certs / "tls.key"),
            "-out",
            str(certs / "tls.crt"),
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
    )
    (certs / "tls.key").chmod(0o644)
    name = "abx-email-live-" + uuid.uuid4().hex[:12]
    password = uuid.uuid4().hex
    subprocess.run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            name,
            "--mount",
            f"type=bind,source={mail},target=/srv/vmail",
            "--mount",
            f"type=bind,source={certs},target=/etc/dovecot/ssl,readonly",
            "--publish",
            "127.0.0.1::31993",
            "--publish",
            "127.0.0.1::31143",
            "--env",
            f"USER_PASSWORD={password}",
            "dovecot/dovecot:2.4.2",
        ],
        check=True,
        capture_output=True,
    )
    previous = os.environ.get("SSL_CERT_FILE")
    os.environ["SSL_CERT_FILE"] = str(certs / "tls.crt")
    try:
        ports = {}
        for inner in (31993, 31143):
            value = subprocess.check_output(["docker", "port", name, str(inner)], text=True).strip()
            ports[inner] = int(value.rsplit(":", 1)[1])
        options = {
            "host": "localhost",
            "port": ports[31993],
            "password": password,
            "tls_mode": "tls",
            "poll_seconds": 5,
        }
        deadline = time.monotonic() + 15
        while True:
            try:
                peer = imaplib.IMAP4_SSL("localhost", ports[31993], ssl_context=ssl.create_default_context())
                peer.login("readiness", password)
                peer.logout()
                break
            except (OSError, imaplib.IMAP4.error):
                if time.monotonic() >= deadline:
                    logs = subprocess.check_output(["docker", "logs", name], text=True)
                    raise RuntimeError(logs)
                time.sleep(0.1)
        yield options, ports[31143], certs / "tls.crt"
    finally:
        subprocess.run(["docker", "rm", "--force", name], check=True, capture_output=True)
        # Dovecot's distroless image has no chown. Linux bind mounts retain its UID after shutdown.
        if os.uname().sysname == "Linux":
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--mount",
                    f"type=bind,source={mail},target=/mail",
                    "alpine:3.22",
                    "chown",
                    "-R",
                    f"{os.getuid()}:{os.getgid()}",
                    "/mail",
                ],
                check=True,
                capture_output=True,
            )
        shutil.rmtree(root)
        if previous is None:
            os.environ.pop("SSL_CERT_FILE", None)
        else:
            os.environ["SSL_CERT_FILE"] = previous


def connect(options):
    peer = imaplib.IMAP4_SSL(options["host"], options["port"], ssl_context=ssl.create_default_context())
    assert peer.login(options["username"], options["password"])[0] == "OK"
    assert peer.select("INBOX")[0] == "OK"
    return peer


def message(text, subject="Inbound acceptance"):
    mail = EmailMessage()
    mail["From"] = "Alice Example <alice@example.org>"
    mail["To"] = "archive@example.org"
    mail["Cc"] = "team@example.org"
    mail["Subject"] = subject
    mail["Message-ID"] = f"<{uuid.uuid4().hex}@example.org>"
    mail.set_content(text)
    return mail


def append(peer, mail, flags=""):
    assert peer.append("INBOX", flags, None, mail.as_bytes())[0] == "OK"


def flags(peer):
    status, rows = peer.uid("FETCH", "1:*", "(FLAGS)")
    assert status == "OK"
    return [row for row in rows if isinstance(row, bytes)]


def bridge(options, store, connection_id="email"):
    connection = Connection(id=connection_id, platform="email", capture={"enabled": True, "options": options})
    settings = Settings(connections=[connection])
    bot = TransportBot(connection, "capture", store)
    engine = Engine(store, settings, connection, None)
    engine.bots["capture"] = bot
    return bot, engine


async def next_job(store, count):
    async with asyncio.timeout(10):
        while True:
            rows = store.db.execute("SELECT payload FROM jobs ORDER BY created_at").fetchall()
            if len(rows) >= count:
                assert len(rows) == count
                return json.loads(rows[-1][0])
            await asyncio.sleep(0.02)


@pytest.mark.parametrize("tls_mode", ["tls", "starttls"])
async def test_real_inbound_mime_cursor_and_flags(imap_server, tmp_path, tls_mode):
    base, starttls_port, _ = imap_server
    options = {**base, "username": "inbound-" + uuid.uuid4().hex, "tls_mode": tls_mode}
    if tls_mode == "starttls":
        options["port"] = starttls_port
    peer_options = {**options, "port": base["port"]}
    peer = connect(peer_options)
    store = Store(tmp_path)
    bot, engine = bridge(options, store)
    try:
        append(peer, message("https://example.org/old"), "\\Seen")
        await bot.check()
        await bot.start(engine.receive)
        await bot.transport.poll()
        assert store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        mail = message("https://example.org/plain\n> Quoted earlier: https://example.org/quoted")
        mail.add_alternative(
            '<p>https://example.org/plain</p><a href="https://example.org/html?a=1&amp;b=2">HTML</a>', subtype="html"
        )
        nested = message("Forwarded https://example.org/nested")
        mail.add_attachment(nested)
        mail.add_attachment("Attachment https://example.org/attachment", subtype="plain", filename="links.txt")
        mail.add_attachment(
            '<a href="https://example.org/html-attachment?a=1&amp;b=2">Saved HTML</a>',
            subtype="html",
            filename="links.html",
        )
        mail.add_attachment(
            message("Saved forward https://example.org/eml-attachment").as_bytes(),
            maintype="application",
            subtype="octet-stream",
            filename="forward.eml",
        )
        mail.add_attachment(
            b"https://example.org/ignored-binary", maintype="application", subtype="pdf", filename="document.pdf"
        )
        append(peer, mail)
        before = flags(peer)
        await bot.transport.poll()
        event = await next_job(store, 1)
        assert set(extract_urls(event["text"])) == {
            "https://example.org/plain",
            "https://example.org/quoted",
            "https://example.org/html?a=1&b=2",
            "https://example.org/nested",
            "https://example.org/attachment",
            "https://example.org/html-attachment?a=1&b=2",
            "https://example.org/eml-attachment",
        }
        assert event["user"] == "alice@example.org"
        assert event["user_name"] == "Alice Example"
        assert event["is_dm"] is True
        assert event["ts"]
        assert flags(peer) == before
        cursors = list((tmp_path / "accounts" / "email:capture").glob("imap-*.json"))
        assert len(cursors) == 1
        saved = json.loads(cursors[0].read_text())
        assert saved["last_uid"] == 2
        assert saved["uidvalidity"]
        assert cursors[0].stat().st_mode & 0o777 == 0o600
        append(peer, mail)
        await bot.transport.poll()
        assert store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 1
        await bot.close()
        bot, engine = bridge(options, store)
        await bot.check()
        await bot.start(engine.receive)
        append(peer, message("https://example.org/after-restart"), "\\Seen")
        await bot.transport.poll()
        next_event = await next_job(store, 2)
        assert extract_urls(next_event["text"]) == ["https://example.org/after-restart"]
        assert next_event["ts"] != event["ts"]
        await bot.transport.poll()
        assert store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 2
    finally:
        await bot.close()
        peer.logout()
        store.db.close()


async def test_real_include_existing_and_invalid_password(imap_server, tmp_path):
    base, _, _ = imap_server
    options = {**base, "username": "existing-" + uuid.uuid4().hex, "include_existing": True}
    peer = connect(options)
    store = Store(tmp_path)
    bot, engine = bridge(options, store)
    try:
        append(peer, message("https://example.org/existing"))
        await bot.check()
        await bot.start(engine.receive)
        event = await next_job(store, 1)
        assert extract_urls(event["text"]) == ["https://example.org/existing"]
        await bot.close()
        bad, bad_engine = bridge({**options, "password": "invalid"}, store, "bad")
        try:
            with pytest.raises(ValueError, match="IMAP sign-in failed"):
                await bad.start(bad_engine.receive)
        finally:
            await bad.close()
    finally:
        await bot.close()
        peer.logout()
        store.db.close()
