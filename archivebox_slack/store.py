"""Small durable inbox/outbox; transactions never span network I/O."""

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .config import SECRET_FIELDS, Settings


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
        values = self.settings().model_dump()
        for key in SECRET_FIELDS:
            values[key] = ""
        values["configured_secrets"] = [key for key in SECRET_FIELDS if getattr(self.settings(), key)]
        return values

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
