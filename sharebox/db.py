"""SQLite 连接管理（线程本地连接 + 显式事务）。"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    username     TEXT NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT NOT NULL DEFAULT '',
    pw_salt      TEXT NOT NULL,
    pw_hash      TEXT NOT NULL,
    pw_iter      INTEGER NOT NULL,
    role         TEXT NOT NULL DEFAULT 'member',
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS folders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    owner_id   INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    is_deleted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS perms (
    user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    level     TEXT NOT NULL,
    PRIMARY KEY (user_id, folder_id)
);

CREATE TABLE IF NOT EXISTS files (
    folder_id  INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    size       INTEGER NOT NULL,
    mtime      REAL NOT NULL,
    sha256     TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by INTEGER,
    deleted    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (folder_id, path)
);

CREATE TABLE IF NOT EXISTS dirs (
    folder_id  INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    path       TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (folder_id, path)
);

CREATE TABLE IF NOT EXISTS changes (
    seq       INTEGER PRIMARY KEY AUTOINCREMENT,
    folder_id INTEGER NOT NULL,
    path      TEXT NOT NULL,
    action    TEXT NOT NULL,
    ts        TEXT NOT NULL,
    actor_id  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_changes_folder ON changes(folder_id, seq);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    device     TEXT NOT NULL DEFAULT '',
    last_seen  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS audit (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    actor_id INTEGER,
    action   TEXT NOT NULL,
    detail   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts);

CREATE TABLE IF NOT EXISTS node (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    node_id       TEXT NOT NULL,
    device_name   TEXT NOT NULL,
    owner_id      INTEGER NOT NULL,
    recovery_salt TEXT NOT NULL,
    recovery_hash TEXT NOT NULL,
    recovery_iter INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    company_id    TEXT NOT NULL,
    name          TEXT NOT NULL,
    code_verifier TEXT NOT NULL,
    code_iter     INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    is_creator    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS peers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id    TEXT NOT NULL UNIQUE,
    company_id TEXT NOT NULL DEFAULT '',
    name       TEXT NOT NULL,
    address    TEXT NOT NULL,
    port       INTEGER NOT NULL DEFAULT 8787,
    token      TEXT NOT NULL,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    last_seen  TEXT,
    last_ip    TEXT
);

CREATE TABLE IF NOT EXISTS mirrors (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_id          INTEGER NOT NULL REFERENCES peers(id) ON DELETE CASCADE,
    remote_folder_id INTEGER NOT NULL,
    remote_name      TEXT NOT NULL,
    local_path       TEXT NOT NULL,
    enabled          INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    UNIQUE(peer_id, remote_folder_id)
);
"""

SCHEMA_VERSION = "2"


class Database:
    def __init__(self, path):
        self.path = str(Path(path))
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False
        self._init()

    # ------------------------------------------------------------------ 连接
    def _new_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_conn()
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None

    # ------------------------------------------------------------------ 初始化
    def _init(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            conn = self.conn()
            conn.executescript(SCHEMA)
            self._migrate(conn)
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (SCHEMA_VERSION,),
            )
            self._initialized = True

    @staticmethod
    def _migrate(conn) -> None:
        """兼容旧数据库：补上后加的列。"""
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(folders)").fetchall()}
        if "local_path" not in columns:
            conn.execute("ALTER TABLE folders ADD COLUMN local_path TEXT")
        peer_columns = {row["name"] for row in conn.execute("PRAGMA table_info(peers)").fetchall()}
        if "company_id" not in peer_columns:
            conn.execute("ALTER TABLE peers ADD COLUMN company_id TEXT NOT NULL DEFAULT ''")

    # ------------------------------------------------------------------ 事务
    @contextmanager
    def tx(self):
        conn = self.conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
