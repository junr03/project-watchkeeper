"""SQLite persistence for observations, projects, decisions, and delivery."""

import json
import os
import sqlite3
from pathlib import Path
from contextlib import contextmanager


SCHEMA_VERSION = 4


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
            conn.execute("PRAGMA secure_delete=ON")
            if self.path != ":memory:" and Path(self.path).exists():
                Path(self.path).chmod(0o600)
            yield conn
        finally:
            conn.close()

    def _initialize(self):
        with self.connect() as c:
            version = c.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, SCHEMA_VERSION):
                raise RuntimeError(f"unsupported database schema version {version}")
            if version == 0:
                c.executescript("""
                    BEGIN IMMEDIATE;
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
                    COMMIT;
                """)
                version = 1
            if version == 1:
                c.execute("BEGIN IMMEDIATE")
                c.execute("ALTER TABLE projects ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
                c.execute("ALTER TABLE projects ADD COLUMN assessment_kind TEXT NOT NULL DEFAULT 'attention'")
                c.execute("ALTER TABLE projects ADD COLUMN eligible INTEGER NOT NULL DEFAULT 1")
                c.execute("ALTER TABLE projects ADD COLUMN eligibility_reason TEXT NOT NULL DEFAULT ''")
                c.execute("ALTER TABLE projects ADD COLUMN assessed_at TEXT")
                c.execute("ALTER TABLE projects ADD COLUMN status TEXT NOT NULL DEFAULT 'explicit_request'")
                c.execute("ALTER TABLE projects ADD COLUMN grouping_confidence TEXT NOT NULL DEFAULT 'strong'")
                c.execute("ALTER TABLE projects ADD COLUMN last_progress_at TEXT")
                c.execute("""CREATE TABLE excluded_sources (
                        source_id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL
                    )""")
                c.execute("""CREATE TABLE excluded_threads (
                        source_id TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        PRIMARY KEY(source_id, thread_id)
                    )""")
                c.execute("""CREATE TABLE thread_ownership (
                        source_id TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        project_id TEXT NOT NULL,
                        assigned_at TEXT NOT NULL,
                        PRIMARY KEY(source_id, thread_id)
                    )""")
                c.execute("CREATE INDEX thread_ownership_project ON thread_ownership(project_id)")
                c.execute("""CREATE TABLE review_proposals (
                        proposal_id TEXT PRIMARY KEY,
                        requested_project_id TEXT NOT NULL,
                        outcome TEXT NOT NULL,
                        next_action TEXT NOT NULL,
                        reasoning TEXT NOT NULL,
                        evidence_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        grouping_confidence TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )""")
                c.execute("PRAGMA user_version=2")
                c.commit()
                version = 2
            if version == 2:
                c.execute("BEGIN IMMEDIATE")
                c.execute("""CREATE TABLE project_threads (
                    source_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    assigned_at TEXT NOT NULL,
                    PRIMARY KEY(source_id,thread_id,project_id)
                )""")
                c.execute("CREATE INDEX project_threads_project ON project_threads(project_id)")
                # Existing projects already carry their captured evidence. Treat
                # those links as discovered associations, never as user locks.
                for project in c.execute("SELECT project_id,evidence_json,updated_at FROM projects").fetchall():
                    for evidence in json.loads(project["evidence_json"]):
                        c.execute("INSERT OR IGNORE INTO project_threads VALUES(?,?,?,?)",
                                  (evidence["source_id"], evidence["thread_id"], project["project_id"], project["updated_at"]))
                c.execute("PRAGMA user_version=3")
                c.commit()
                version = 3
            if version == 3:
                c.execute("BEGIN IMMEDIATE")
                c.execute("""CREATE TABLE expired_observations (
                    observation_id TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    first_captured_at TEXT NOT NULL,
                    expired_at TEXT NOT NULL,
                    PRIMARY KEY(observation_id,revision)
                )""")
                c.execute("CREATE INDEX expired_observations_thread ON expired_observations(source_id,thread_id)")
                c.execute("""CREATE TABLE thread_progress_watermarks (
                    source_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    last_progress_at TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    last_captured_at TEXT NOT NULL,
                    PRIMARY KEY(source_id,thread_id)
                )""")
                c.execute("""CREATE TABLE privacy_journal_state (
                    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
                    last_seq INTEGER NOT NULL
                )""")
                c.execute("INSERT INTO privacy_journal_state VALUES(1,0)")
                c.execute("PRAGMA user_version=4")
                c.commit()

    @staticmethod
    def row_dict(row):
        return dict(row) if row else None

    @staticmethod
    def json(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
