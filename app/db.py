"""SQLite 永続化レイヤ。"""
from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, time
from pathlib import Path
from typing import Iterable, Optional

from .models import Event, Notification

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    date TEXT NOT NULL,
    start_time TEXT,
    end_time TEXT,
    location TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'confirmed',
    source TEXT NOT NULL DEFAULT 'manual',
    source_message_id TEXT NOT NULL DEFAULT '',
    source_author TEXT NOT NULL DEFAULT '',
    source_channel TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    raw_text TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(date);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    event_id INTEGER,
    read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_messages (
    source TEXT NOT NULL,
    message_id TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (source, message_id)
);
"""


def _t(value: Optional[str]) -> Optional[time]:
    return time.fromisoformat(value) if value else None


def _row_to_event(row: sqlite3.Row) -> Event:
    return Event(
        id=row["id"],
        title=row["title"],
        date=date.fromisoformat(row["date"]),
        start_time=_t(row["start_time"]),
        end_time=_t(row["end_time"]),
        location=row["location"],
        description=row["description"],
        status=row["status"],
        source=row["source"],
        source_message_id=row["source_message_id"],
        source_author=row["source_author"],
        source_channel=row["source_channel"],
        source_url=row["source_url"],
        raw_text=row["raw_text"],
        confidence=row["confidence"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_notification(row: sqlite3.Row) -> Notification:
    return Notification(
        id=row["id"],
        kind=row["kind"],
        message=row["message"],
        event_id=row["event_id"],
        read=bool(row["read"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


class Database:
    def __init__(self, path: Path | str, tz=None):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._tz = tz
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _now(self) -> str:
        return datetime.now(self._tz).isoformat(timespec="seconds")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---------- events ----------
    def add_event(self, ev: Event) -> Event:
        with self._lock:
            created = self._now()
            cur = self._conn.execute(
                """INSERT INTO events (title, date, start_time, end_time, location, description, status,
                   source, source_message_id, source_author, source_channel, source_url, raw_text,
                   confidence, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ev.title,
                    ev.date.isoformat(),
                    ev.start_time.strftime("%H:%M") if ev.start_time else None,
                    ev.end_time.strftime("%H:%M") if ev.end_time else None,
                    ev.location,
                    ev.description,
                    ev.status,
                    ev.source,
                    ev.source_message_id,
                    ev.source_author,
                    ev.source_channel,
                    ev.source_url,
                    ev.raw_text,
                    ev.confidence,
                    created,
                ),
            )
            self._conn.commit()
            ev.id = cur.lastrowid
            ev.created_at = datetime.fromisoformat(created)
            return ev

    def get_event(self, event_id: int) -> Optional[Event]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return _row_to_event(row) if row else None

    def list_events(
        self,
        statuses: Iterable[str] = ("confirmed",),
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        on_date: Optional[date] = None,
    ) -> list[Event]:
        statuses = list(statuses)
        sql = f"SELECT * FROM events WHERE status IN ({','.join('?' * len(statuses))})"
        params: list = list(statuses)
        if on_date:
            sql += " AND date = ?"
            params.append(on_date.isoformat())
        if date_from:
            sql += " AND date >= ?"
            params.append(date_from.isoformat())
        if date_to:
            sql += " AND date <= ?"
            params.append(date_to.isoformat())
        sql += " ORDER BY date, start_time IS NOT NULL, start_time, id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_event(r) for r in rows]

    def set_status(self, event_id: int, status: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE events SET status = ? WHERE id = ?", (status, event_id))
            self._conn.commit()

    def delete_event(self, event_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
            self._conn.commit()
            return cur.rowcount > 0

    # ---------- notifications ----------
    def add_notification(self, n: Notification) -> Notification:
        with self._lock:
            created = self._now()
            cur = self._conn.execute(
                "INSERT INTO notifications (kind, message, event_id, read, created_at) VALUES (?,?,?,?,?)",
                (n.kind, n.message, n.event_id, int(n.read), created),
            )
            self._conn.commit()
            n.id = cur.lastrowid
            n.created_at = datetime.fromisoformat(created)
            return n

    def list_notifications(self, limit: int = 100) -> list[Notification]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM notifications ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_notification(r) for r in rows]

    def unread_count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM notifications WHERE read = 0").fetchone()[0]

    def mark_all_read(self) -> None:
        with self._lock:
            self._conn.execute("UPDATE notifications SET read = 1 WHERE read = 0")
            self._conn.commit()

    # ---------- processed messages ----------
    def mark_processed(self, source: str, message_id: str) -> bool:
        """未処理なら記録して True、既に処理済みなら False。"""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO processed_messages (source, message_id, processed_at) VALUES (?,?,?)",
                    (source, message_id, self._now()),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def unmark_processed(self, source: str, message_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM processed_messages WHERE source = ? AND message_id = ?", (source, message_id)
            )
            self._conn.commit()
