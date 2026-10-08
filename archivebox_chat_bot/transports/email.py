"""Inbound-only IMAP: read-only mailboxes, durable UID cursor, standard-library MIME."""

import asyncio
import contextlib
import hashlib
import imaplib
import json
import logging
import os
import re
import ssl
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from html.parser import HTMLParser
from pathlib import Path

from ..text import extract_urls

log = logging.getLogger(__name__)


class _HTMLLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fragments = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag == "a" and not self.hidden:
            self.fragments.extend(value for key, value in attrs if key == "href" and value)

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.fragments.append(data)


def email_message(raw, folder):
    """Include quoted chains and text/HTML/.eml attachments, never execute or fetch MIME content."""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    display, sender = parseaddr(str(message.get("From", "")))
    if not sender or "@" not in sender:
        sender, display = "unknown", "Unknown sender"
    texts = []
    parts = [message]
    count = 0
    while parts:
        part = parts.pop()
        count += 1
        if count > 1000:
            raise ValueError("Email has too many MIME parts")
        if part.get("Subject"):
            texts.append(str(part["Subject"]))
        if part.is_multipart():
            parts.extend(reversed(part.get_payload()))
            continue
        payload = part.get_payload(decode=True) or b""
        filename = (part.get_filename() or "").lower()
        content_type = part.get_content_type()
        if filename.endswith(".eml") and content_type != "message/rfc822":
            parts.append(BytesParser(policy=policy.default).parsebytes(payload))
            continue
        if part.get_content_maintype() != "text" and not filename.endswith((".txt", ".html", ".htm", ".md", ".csv")):
            continue
        try:
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except LookupError:
            text = payload.decode("utf-8", errors="replace")
        if content_type == "text/html" or filename.endswith((".html", ".htm")):
            parser = _HTMLLinks()
            parser.feed(text)
            text = "\n".join(parser.fragments)
        texts.append(text)
    urls = extract_urls("\n".join(texts))
    return {
        "id": hashlib.sha256(raw).hexdigest(),
        "channel": folder,
        "user": sender.lower(),
        "user_name": display or sender.lower(),
        "text": "\n".join(urls),
        "is_dm": True,
    }


class EmailTransport:
    def __init__(self, options, account_id, data_dir, emit):
        self.options, self.emit = options, emit
        self.host = str(options.get("host", "")).strip()
        self.username = str(options.get("username", "")).strip()
        self.folder = str(options.get("folder") or "INBOX")
        self.tls_mode = options.get("tls_mode", "tls")
        self.port = int(options.get("port") or (143 if self.tls_mode == "starttls" else 993))
        self.interval = int(options.get("poll_seconds", 30))
        self.max_bytes = int(options.get("max_message_mb", 25)) * 1024 * 1024
        if not self.host or not self.username or not options.get("password"):
            raise ValueError("Enter your IMAP host, mailbox address and app password")
        if self.tls_mode not in {"tls", "starttls"}:
            raise ValueError("Choose TLS or STARTTLS for your IMAP connection")
        if not 1 <= self.port <= 65535 or not 5 <= self.interval <= 3600 or not 1 <= self.max_bytes // 1024**2 <= 100:
            raise ValueError("Use port 1–65535, polling 5–3600 seconds, and message limit 1–100 MB")
        # Cursor and durable identity follow the mailbox, not its password or UI display name.
        self.identity = hashlib.sha256(
            json.dumps([self.host.lower(), self.port, self.username.lower(), self.folder, self.tls_mode]).encode()
        ).hexdigest()
        self.path = Path(data_dir) / f"imap-{self.identity}.json"
        self.state = json.loads(self.path.read_text()) if self.path.exists() else None
        self.initialized = False
        self.task = None
        self.error = ""
        self.lock = asyncio.Lock()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with os.fdopen(os.open(temporary, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w") as stream:
            json.dump(self.state, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.path)

    def _read(self, initial_only=False):
        context = ssl.create_default_context()
        client = None
        try:
            if self.tls_mode == "tls":
                client = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=context, timeout=20)
            else:
                client = imaplib.IMAP4(self.host, self.port, timeout=20)
                client.starttls(ssl_context=context)
            client.login(self.username, self.options["password"])
            status, _ = client.select('"' + self.folder.replace("\\", "\\\\").replace('"', '\\"') + '"', readonly=True)
            if status != "OK":
                raise ValueError("IMAP folder not found. Check its exact name in your mail provider.")
            validity = client.response("UIDVALIDITY")[1]
            next_uid = client.response("UIDNEXT")[1]
            if not validity or not validity[0] or not next_uid or not next_uid[0]:
                raise ValueError("The IMAP server did not return a stable mailbox UID cursor")
            epoch = validity[0].decode()
            state = self.state
            if state is None:
                state = {
                    "uidvalidity": epoch,
                    "last_uid": 0 if self.options.get("include_existing") else int(next_uid[0]) - 1,
                }
            elif state["uidvalidity"] != epoch:
                # Renumbered mailboxes must be rescanned. Content IDs deduplicate already queued messages.
                state = {"uidvalidity": epoch, "last_uid": 0}
            else:
                state = dict(state)
            if initial_only:
                return state, []
            status, rows = client.uid("search", None, "UID", f"{state['last_uid'] + 1}:*")
            if status != "OK":
                raise ValueError("IMAP could not list new messages")
            # IMAP's n:* range includes the last message even when n is above UIDNEXT.
            uids = sorted(int(uid) for uid in rows[0].split() if int(uid) > state["last_uid"])[:50]
            batch, total = [], 0
            for uid in uids:
                status, metadata = client.uid("fetch", str(uid), "(RFC822.SIZE)")
                if status != "OK":
                    if self._uid_exists(client, uid):
                        raise ValueError(f"IMAP returned an incomplete size response for message UID {uid}")
                    batch.append((uid, None))
                    continue
                size = next(
                    (
                        int(m[1])
                        for item in metadata
                        if isinstance(item, bytes) and (m := re.search(rb"RFC822.SIZE (\d+)", item))
                    ),
                    None,
                )
                if size is None:
                    # A UID can disappear between SEARCH and FETCH. Preserve the cursor
                    # when the server still lists it; some servers return partial FETCH
                    # responses while a new message is being made available.
                    if self._uid_exists(client, uid):
                        raise ValueError(f"IMAP returned no size for message UID {uid}")
                    batch.append((uid, None))
                    continue
                if size > self.max_bytes:
                    if batch:
                        break
                    raise ValueError(
                        f"Email UID {uid} exceeds the {self.max_bytes // 1024**2} MB limit; increase Maximum email size"
                    )
                if batch and total + size > self.max_bytes:
                    break
                status, body = client.uid("fetch", str(uid), "(BODY.PEEK[])")
                if status != "OK":
                    if self._uid_exists(client, uid):
                        raise ValueError(f"IMAP returned an incomplete body response for message UID {uid}")
                    batch.append((uid, None))
                    continue
                raw = next((row[1] for row in body if isinstance(row, tuple)), None)
                if not raw:
                    if self._uid_exists(client, uid):
                        raise ValueError(f"IMAP returned no body for message UID {uid}")
                    batch.append((uid, None))
                    continue
                batch.append((uid, raw))
                total += size
            return state, batch
        except ssl.SSLCertVerificationError:
            raise ValueError(
                "IMAP TLS certificate could not be verified. Check the server hostname and certificate."
            ) from None
        except imaplib.IMAP4.error:
            raise ValueError(
                "IMAP sign-in failed or the server rejected the request. Check your mailbox app password and IMAP access."
            ) from None
        except OSError:
            raise ValueError("Cannot reach the IMAP server. Check its hostname, port and TLS setting.") from None
        finally:
            if client:
                with contextlib.suppress(imaplib.IMAP4.error, OSError):
                    client.logout()

    @staticmethod
    def _uid_exists(client, uid):
        status, rows = client.uid("search", None, "UID", str(uid))
        if status != "OK":
            raise ValueError(f"IMAP could not verify whether message UID {uid} still exists")
        return any(int(value) == uid for value in rows[0].split())

    async def check(self):
        if not self.initialized:
            self.state, _ = await asyncio.to_thread(self._read, True)
            self._save()
            self.initialized = True
            log.info("Email inbox ready at UID %s", self.state["last_uid"])
        return {
            "id": self.identity,
            "name": self.username,
            "capabilities": {"reactions": False, "media": False, "threads": False, "inbound_only": True},
        }

    async def start(self):
        await self.check()
        self.task = asyncio.create_task(self.run())

    async def poll(self):
        async with self.lock:
            await self.check()
            state, batch = await asyncio.to_thread(self._read)
            self.state = state
            self._save()
            for uid, raw in batch:
                if raw:
                    log.info("Email received UID %s (%s bytes)", uid, len(raw))
                    await self.emit(email_message(raw, self.folder))
                    log.info("Email UID %s accepted by the message handler", uid)
                else:
                    log.info("Email UID %s was expunged before retrieval", uid)
                # Advance only after the shared engine durably accepts or filters this message.
                self.state["last_uid"] = uid
                self._save()
            self.error = ""

    async def run(self):
        while True:
            try:
                await self.poll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Email inbox polling failed")
                self.error = str(exc)
            await asyncio.sleep(self.interval)

    async def channels(self):
        return [{"id": self.folder, "name": self.folder, "is_dm": True}]

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
