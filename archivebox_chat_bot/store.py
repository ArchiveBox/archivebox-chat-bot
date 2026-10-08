"""Small durable inbox/outbox; transactions never span network I/O."""

import json
import os
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings, is_secret, public_settings
from .text import extract_urls


def now():
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.directory / "bridge.sqlite3")
        os.chmod(self.directory / "bridge.sqlite3", 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                level TEXT NOT NULL, kind TEXT NOT NULL, connection TEXT NOT NULL, role TEXT NOT NULL,
                message TEXT NOT NULL, status_code INTEGER, elapsed_ms REAL);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL DEFAULT 'queued', result TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS history (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, connection TEXT NOT NULL, role TEXT NOT NULL,
                channel TEXT NOT NULL, message_id TEXT NOT NULL, thread TEXT NOT NULL, user_id TEXT NOT NULL,
                user_name TEXT NOT NULL, text TEXT NOT NULL, is_bot INTEGER NOT NULL,
                UNIQUE(connection,role,channel,message_id));
            CREATE INDEX IF NOT EXISTS history_channel ON history(connection,role,channel,seq);
            CREATE TABLE IF NOT EXISTS groups (
                connection TEXT NOT NULL, channel TEXT NOT NULL, name TEXT NOT NULL,
                is_dm INTEGER NOT NULL DEFAULT 0, auto_archive INTEGER,
                PRIMARY KEY(connection,channel));
            CREATE TABLE IF NOT EXISTS identity_bindings (
                connection TEXT NOT NULL, role TEXT NOT NULL, fingerprint TEXT NOT NULL,
                PRIMARY KEY(connection,role));
            CREATE TABLE IF NOT EXISTS identity_quarantine (
                namespace TEXT PRIMARY KEY, connection TEXT NOT NULL, role TEXT NOT NULL,
                previous_fingerprint TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, created_at);
        """)
        if "scope" not in {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN scope TEXT NOT NULL DEFAULT ''")
        if "received_at" not in {row[1] for row in self.db.execute("PRAGMA table_info(history)")}:
            self.db.execute("ALTER TABLE history ADD COLUMN received_at TEXT NOT NULL DEFAULT ''")
        self.db.execute("CREATE INDEX IF NOT EXISTS events_connection ON events(connection,id)")
        self.db.execute("CREATE INDEX IF NOT EXISTS jobs_connection ON jobs(json_extract(payload,'$.connection'))")
        self.db.execute(
            "UPDATE jobs SET state='uncertain', error='Service restarted during a remote operation; inspect before retrying.' "
            "WHERE state='running'"
        )
        self.db.commit()

    def settings(self) -> Settings:
        row = self.db.execute("SELECT value FROM settings WHERE id=1").fetchone()
        return Settings.model_validate_json(row[0]) if row else Settings()

    def save_settings(self, settings: Settings):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (1,?)", (settings.model_dump_json(),))

    def public_settings(self):
        return public_settings(self.settings().model_dump())

    def redact(self, message, *, settings=None):
        def secrets(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if is_secret(key) and isinstance(item, str) and item:
                        yield item
                    else:
                        yield from secrets(item)
            elif isinstance(value, list):
                for item in value:
                    yield from secrets(item)

        text = str(message)
        values = set(secrets(settings if settings is not None else self.settings().model_dump()))
        if os.environ.get("ADMIN_PASSWORD"):
            values.add(os.environ["ADMIN_PASSWORD"])
        for value in sorted(values, key=len, reverse=True):
            text = text.replace(value, "[redacted]")
        # Errors may contain credentials in URLs or authorization headers.
        text = re.sub(r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", text)
        text = re.sub(r"(?i)([?&](?:token|key|secret|password|code|access_token)=)[^\s&#]+", r"\1[redacted]", text)
        text = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/-]+=*", r"\1 [redacted]", text)
        return text[:2000]

    def event(self, kind, message, *, level="info", connection="", role="", status_code=None, elapsed_ms=None):
        message = self.redact(message)
        with self.db:
            self.db.execute(
                "INSERT INTO events(created_at,level,kind,connection,role,message,status_code,elapsed_ms) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (now(), level, kind, connection, role, message, status_code, elapsed_ms),
            )
            self.db.execute("DELETE FROM events WHERE id <= (SELECT MAX(id)-10000 FROM events)")

    def events(self, limit=200, before=None, connection=None):
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM events WHERE (? IS NULL OR id < ?) AND (? IS NULL OR connection=?) ORDER BY id DESC LIMIT ?",
                (before, before, connection, connection, min(max(limit, 1), 500)),
            ).fetchall()
        ]

    def connection_activity(self, connection):
        stats = dict(
            self.db.execute(
                "SELECT COALESCE(SUM(CASE WHEN state='done' THEN json_array_length(result,'$.urls') ELSE 0 END),0) AS urls_saved, "
                "MAX(created_at) AS last_message_at FROM jobs WHERE kind='message' AND json_extract(payload,'$.connection')=?",
                (connection,),
            ).fetchone()
        )
        received = self.db.execute(
            "SELECT MAX(received_at) FROM history WHERE connection=? AND is_bot=0", (connection,)
        ).fetchone()[0]
        stats["last_message_at"] = max(filter(None, [received, stats["last_message_at"]]), default=None)
        return {**stats, "events": self.events(limit=50, connection=connection), "people": self.people(connection)}

    def people(self, connection):
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT user_id AS id, MAX(user_name) AS name FROM history WHERE connection=? AND is_bot=0 GROUP BY user_id ORDER BY name",
                (connection,),
            ).fetchall()
        ]

    def export_connection(self, connection):
        # Materialize SQLite results before returning a response; no cursor or
        # transaction stays open while the browser downloads the CSV.
        rows = [
            dict(row)
            for row in self.db.execute(
                "SELECT created_at,role,level,kind,message,status_code,elapsed_ms FROM events WHERE connection=? ORDER BY id",
                (connection,),
            ).fetchall()
        ]
        job_messages = set()
        for row in self.db.execute(
            "SELECT created_at,state,error,payload,result FROM jobs WHERE json_extract(payload,'$.connection')=? ORDER BY created_at",
            (connection,),
        ).fetchall():
            payload, result = json.loads(row["payload"]), json.loads(row["result"])
            job_messages.add((payload.get("role"), payload.get("channel"), payload.get("ts")))
            for url in result.get("urls") or extract_urls(payload.get("text", "")):
                rows.append(
                    {
                        "created_at": row["created_at"],
                        "role": payload.get("role", ""),
                        "kind": "url",
                        "state": row["state"],
                        "message": row["error"],
                        "url": url,
                        "user": payload.get("user", ""),
                        "channel": payload.get("channel", ""),
                        "crawl_id": result.get("crawl_id", ""),
                    }
                )
        for row in self.db.execute(
            "SELECT received_at,role,channel,message_id,user_id,text FROM history WHERE connection=? AND is_bot=0 ORDER BY seq",
            (connection,),
        ).fetchall():
            if (row["role"], row["channel"], row["message_id"]) in job_messages:
                continue
            for url in extract_urls(row["text"]):
                rows.append(
                    {
                        "created_at": row["received_at"],
                        "role": row["role"],
                        "kind": "url",
                        "state": "observed",
                        "url": url,
                        "user": row["user_id"],
                        "channel": row["channel"],
                    }
                )
        return rows

    def bind_identity(self, connection: str, role: str, fingerprint: str):
        """Bind verified source identity before reading policy/history or remembering.

        Fingerprints must include the upstream server and authenticated account,
        never credentials. Each role has its own binding; an account replacement
        invalidates shared group policy conservatively. First binding quarantines
        unknown legacy history, but adding the second role preserves newly bound
        group policy. Quarantined rows remain recoverable using identity_quarantine.
        """
        if not connection or role not in ("capture", "ai") or not fingerprint:
            raise ValueError("A connection, bot role and verified identity are required")
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            previous = self.db.execute(
                "SELECT fingerprint FROM identity_bindings WHERE connection=? AND role=?",
                (connection, role),
            ).fetchone()
            if previous and previous[0] == fingerprint:
                return
            reset_groups = (
                previous is not None
                or not self.db.execute(
                    "SELECT 1 FROM identity_bindings WHERE connection=? LIMIT 1", (connection,)
                ).fetchone()
            )
            namespace = "quarantine:" + uuid.uuid4().hex
            moved_history = self.db.execute(
                "UPDATE history SET connection=? WHERE connection=? AND role=?",
                (namespace, connection, role),
            ).rowcount
            moved_groups = 0
            if reset_groups:
                moved_groups = self.db.execute(
                    "UPDATE groups SET connection=? WHERE connection=?", (namespace, connection)
                ).rowcount
            if moved_history or moved_groups:
                self.db.execute(
                    "INSERT INTO identity_quarantine VALUES(?,?,?,?,?,?)",
                    (namespace, connection, role, previous[0] if previous else "", fingerprint, now()),
                )
            self.db.execute("INSERT OR REPLACE INTO identity_bindings VALUES(?,?,?)", (connection, role, fingerprint))

    def remember(self, connection, message):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO history(connection,role,channel,message_id,thread,user_id,user_name,text,is_bot,received_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    connection,
                    message.role,
                    message.channel,
                    message.ts,
                    message.thread,
                    message.user,
                    message.user_name,
                    message.text,
                    int(message.is_bot),
                    now(),
                ),
            )
            self.db.execute(
                "DELETE FROM history WHERE connection=? AND role=? AND channel=? AND seq NOT IN "
                "(SELECT seq FROM history WHERE connection=? AND role=? AND channel=? ORDER BY seq DESC LIMIT 200)",
                (connection, message.role, message.channel) * 2,
            )
            self.db.execute(
                "INSERT OR IGNORE INTO groups(connection,channel,name,is_dm) VALUES(?,?,?,?)",
                (connection, message.channel, message.channel_name or message.channel, int(message.is_dm)),
            )
            if message.channel_name:
                self.db.execute(
                    "UPDATE groups SET name=? WHERE connection=? AND channel=?",
                    (message.channel_name, connection, message.channel),
                )

    def recent(self, connection, message):
        rows = self.db.execute(
            "SELECT text FROM history WHERE connection=? AND role=? AND channel=? AND message_id!=? "
            "AND (?='' OR thread=? OR message_id=?) AND (is_bot=0 OR ?='ai') "
            "AND seq < COALESCE((SELECT seq FROM history WHERE connection=? AND role=? AND channel=? AND message_id=?),9223372036854775807) "
            "ORDER BY seq DESC LIMIT 10",
            (
                connection,
                message.role,
                message.channel,
                message.ts,
                message.thread,
                message.thread,
                message.thread,
                message.role,
                connection,
                message.role,
                message.channel,
                message.ts,
            ),
        ).fetchall()
        return [row[0] for row in reversed(rows)]

    def groups(self, connection):
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM groups WHERE connection=? ORDER BY name", (connection,)
            ).fetchall()
        ]

    def group_policy(self, connection, channel, default=False):
        row = self.db.execute(
            "SELECT auto_archive FROM groups WHERE connection=? AND channel=?", (connection, channel)
        ).fetchone()
        return bool(row[0]) if row and row[0] is not None else default

    def set_group(self, connection, channel, *, name=None, auto_archive=None, is_dm=False):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO groups(connection,channel,name,is_dm) VALUES(?,?,?,?)",
                (connection, channel, name or channel, int(is_dm)),
            )
            if name is not None:
                self.db.execute(
                    "UPDATE groups SET name=? WHERE connection=? AND channel=?", (name, connection, channel)
                )
            if auto_archive is not None:
                self.db.execute(
                    "UPDATE groups SET auto_archive=? WHERE connection=? AND channel=?",
                    (int(auto_archive), connection, channel),
                )

    def meta(self, key, default=""):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, str(value)))

    def enqueue(self, key, kind, payload, scope=""):
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO jobs(id,kind,payload,scope,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (key, kind, json.dumps(payload), scope, now(), now()),
            )
        return bool(cursor.rowcount)

    def update(self, key, state, result=None, error=""):
        previous = self.get(key)
        with self.db:
            if result is None:
                self.db.execute("UPDATE jobs SET state=?,error=?,updated_at=? WHERE id=?", (state, error, now(), key))
            else:
                self.db.execute(
                    "UPDATE jobs SET state=?,result=?,error=?,updated_at=? WHERE id=?",
                    (state, json.dumps(result), error, now(), key),
                )
        if previous and previous["state"] != state and (connection := previous["payload"].get("connection")):
            self.event(
                "job",
                f"{previous['kind']}: {state}" + (f" · {error}" if error else ""),
                connection=connection,
                role=previous["payload"].get("role", "capture"),
                level="error" if state in ("failed", "uncertain") else "info",
            )

    def jobs(self, state=None, limit=100, scope=None):
        clauses, values = [], []
        if state:
            clauses.append("state=?")
            values.append(state)
        if scope is not None:
            clauses.append("scope=?")
            values.append(scope)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        ordering = "ASC" if state else "DESC"
        rows = self.db.execute(
            f"SELECT * FROM jobs{where} ORDER BY created_at {ordering} LIMIT ?", (*values, limit)
        ).fetchall()
        return [
            {**dict(row), "payload": json.loads(row["payload"]), "result": json.loads(row["result"])} for row in rows
        ]

    def get(self, key):
        row = self.db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone()
        return (
            {**dict(row), "payload": json.loads(row["payload"]), "result": json.loads(row["result"])} if row else None
        )

    def stats(self):
        return dict(self.db.execute("SELECT state,count(*) FROM jobs GROUP BY state").fetchall())

    def close(self):
        self.db.close()
