"""SQLite: сообщения, сохранённые материалы, сводки, key-value."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
    chat_id INTEGER, msg_id INTEGER, date INTEGER,
    sender_id INTEGER, sender TEXT, text TEXT, media TEXT,
    reply_to INTEGER, grouped_id INTEGER, forwarded INTEGER DEFAULT 0,
    PRIMARY KEY(chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(chat_id, date);
CREATE TABLE IF NOT EXISTS materials(
    chat_id INTEGER, msg_id INTEGER, created INTEGER,
    subject TEXT, kind TEXT, title TEXT, path TEXT,
    PRIMARY KEY(chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_materials_created ON materials(created);
CREATE TABLE IF NOT EXISTS summaries(
    id INTEGER PRIMARY KEY AUTOINCREMENT, created INTEGER,
    ts_from INTEGER, ts_to INTEGER, text TEXT
);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    # --- messages ---
    def add_message(self, row: dict) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO messages(chat_id,msg_id,date,sender_id,sender,text,media,reply_to,grouped_id,forwarded)"
            " VALUES(:chat_id,:msg_id,:date,:sender_id,:sender,:text,:media,:reply_to,:grouped_id,:forwarded)",
            row,
        )
        self.db.commit()
        return cur.rowcount > 0

    def update_text(self, chat_id: int, msg_id: int, text: str) -> None:
        self.db.execute("UPDATE messages SET text=? WHERE chat_id=? AND msg_id=?", (text, chat_id, msg_id))
        self.db.commit()

    def last_msg_id(self, chat_id: int) -> int | None:
        r = self.db.execute("SELECT MAX(msg_id) FROM messages WHERE chat_id=?", (chat_id,)).fetchone()
        return r[0]

    def messages_between(self, chat_id: int, ts_from: float, ts_to: float) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM messages WHERE chat_id=? AND date>=? AND date<? ORDER BY date, msg_id",
            (chat_id, int(ts_from), int(ts_to)),
        ).fetchall()

    def context_before(self, chat_id: int, msg_id: int, limit: int = 12) -> list[sqlite3.Row]:
        rows = self.db.execute(
            "SELECT * FROM messages WHERE chat_id=? AND msg_id<? ORDER BY msg_id DESC LIMIT ?",
            (chat_id, msg_id, limit),
        ).fetchall()
        return list(reversed(rows))

    def group_messages(self, chat_id: int, grouped_id: int) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM messages WHERE chat_id=? AND grouped_id=? ORDER BY msg_id", (chat_id, grouped_id)
        ).fetchall()

    def get_message(self, chat_id: int, msg_id: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM messages WHERE chat_id=? AND msg_id=?", (chat_id, msg_id)
        ).fetchone()

    def count_messages_since(self, ts: float) -> int:
        return self.db.execute("SELECT COUNT(*) FROM messages WHERE date>=?", (int(ts),)).fetchone()[0]

    # --- materials ---
    def material_exists(self, chat_id: int, msg_id: int) -> bool:
        return self.db.execute(
            "SELECT 1 FROM materials WHERE chat_id=? AND msg_id=?", (chat_id, msg_id)
        ).fetchone() is not None

    def add_material(self, chat_id, msg_id, subject, kind, title, path) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO materials VALUES(?,?,?,?,?,?,?)",
            (chat_id, msg_id, int(time.time()), subject, kind, title, path),
        )
        self.db.commit()

    def materials_since(self, ts: float) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM materials WHERE created>=? ORDER BY created", (int(ts),)
        ).fetchall()

    def last_materials(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM materials ORDER BY created DESC LIMIT ?", (limit,)
        ).fetchall()

    # --- summaries ---
    def add_summary(self, ts_from, ts_to, text) -> None:
        self.db.execute(
            "INSERT INTO summaries(created,ts_from,ts_to,text) VALUES(?,?,?,?)",
            (int(time.time()), int(ts_from), int(ts_to), text),
        )
        self.db.commit()

    def last_summary(self) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM summaries ORDER BY id DESC LIMIT 1").fetchone()

    # --- kv ---
    def kv_get(self, key: str, default: str | None = None) -> str | None:
        r = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def kv_set(self, key: str, value) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (key, str(value)))
        self.db.commit()
