"""Small durable inbox/outbox; transactions never span network I/O."""

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .config import Settings, public_settings


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
            CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, created_at);
        """)
        if "scope" not in {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN scope TEXT NOT NULL DEFAULT ''")
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

    def remember(self, connection, message):
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO history(connection,role,channel,message_id,thread,user_id,user_name,text,is_bot) VALUES(?,?,?,?,?,?,?,?,?)",
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
                ),
            )
            self.db.execute(
                "DELETE FROM history WHERE connection=? AND role=? AND channel=? AND seq NOT IN "
                "(SELECT seq FROM history WHERE connection=? AND role=? AND channel=? ORDER BY seq DESC LIMIT 200)",
                (connection, message.role, message.channel) * 2,
            )
            self.db.execute(
                "INSERT OR IGNORE INTO groups(connection,channel,name,is_dm) VALUES(?,?,?,?)",
                (connection, message.channel, message.channel, int(message.is_dm)),
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
        with self.db:
            if result is None:
                self.db.execute("UPDATE jobs SET state=?,error=?,updated_at=? WHERE id=?", (state, error, now(), key))
            else:
                self.db.execute(
                    "UPDATE jobs SET state=?,result=?,error=?,updated_at=? WHERE id=?",
                    (state, json.dumps(result), error, now(), key),
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
