"""SQLite persistence for observations, projects, decisions, and delivery."""

import json
import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager


SCHEMA_VERSION = 1


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            parent = Path(self.path).parent
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            Path(self.path).chmod(0o600)
        self._initialize()

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            if self.path != ":memory:" and Path(self.path).exists():
                Path(self.path).chmod(0o600)
            yield conn
        finally:
            conn.close()

    def _initialize(self):
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            version = c.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise RuntimeError(f"unsupported database schema version {version}")
            if version == 0:
                c.executescript("""
                    CREATE TABLE observations (
                        observation_id TEXT PRIMARY KEY,
                        source_id TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        revision TEXT NOT NULL,
                        source_timestamp TEXT NOT NULL,
                        captured_at TEXT NOT NULL,
                        locator TEXT NOT NULL,
                        text TEXT NOT NULL,
                        UNIQUE(source_id, thread_id, message_id)
                    );
                    CREATE TABLE sources (
                        source_id TEXT PRIMARY KEY,
                        cursor TEXT,
                        last_seen_at TEXT,
                        available INTEGER NOT NULL DEFAULT 0,
                        last_error TEXT
                    );
                    CREATE TABLE projects (
                        project_id TEXT PRIMARY KEY,
                        outcome TEXT NOT NULL,
                        next_action TEXT NOT NULL,
                        reasoning TEXT NOT NULL,
                        evidence_json TEXT NOT NULL,
                        fingerprint TEXT NOT NULL,
                        disposition TEXT NOT NULL DEFAULT 'open',
                        snooze_until TEXT,
                        updated_at TEXT NOT NULL
                    );
                    CREATE TABLE outbox (
                        delivery_id TEXT PRIMARY KEY,
                        project_id TEXT NOT NULL,
                        fingerprint TEXT NOT NULL UNIQUE,
                        created_at TEXT NOT NULL,
                        status TEXT NOT NULL,
                        delivered_at TEXT
                    );
                    PRAGMA user_version=1;
                """)
                c.commit()
            else:
                c.commit()

    @staticmethod
    def row_dict(row):
        return dict(row) if row else None

    @staticmethod
    def json(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
