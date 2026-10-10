"""SQLite 永続化レイヤ。"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime, time
from pathlib import Path
from typing import Iterable, Optional

from .models import Event, EventChange, ImageInput, IncomingMessage, Notification

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
    created_at TEXT NOT NULL,
    updated_at TEXT,
    gcal_event_id TEXT,
    gcal_hash TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_date ON events(date);

CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    event_id INTEGER,
    change_id INTEGER,
    read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reason TEXT NOT NULL DEFAULT '',
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL DEFAULT '{}',
    source TEXT NOT NULL DEFAULT 'manual',
    source_message_id TEXT NOT NULL DEFAULT '',
    source_author TEXT NOT NULL DEFAULT '',
    source_channel TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    raw_text TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 1.0,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_changes_event ON event_changes(event_id);

-- ローカルLLM で精査できなかったメッセージ（あとで自動で再精査する）
CREATE TABLE IF NOT EXISTS message_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    message_id TEXT NOT NULL,
    text TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'waiting',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE (source, message_id)
);
CREATE TABLE IF NOT EXISTS message_queue_images (
    queue_id INTEGER NOT NULL,
    idx INTEGER NOT NULL,
    mime TEXT NOT NULL,
    filename TEXT NOT NULL DEFAULT '',
    data BLOB NOT NULL,
    PRIMARY KEY (queue_id, idx)
);

-- Google カレンダーから消す必要がある予定（アプリ側で削除された予定）
CREATE TABLE IF NOT EXISTS gcal_tombstones (
    gcal_event_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_messages (
    source TEXT NOT NULL,
    message_id TEXT NOT NULL,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (source, message_id)
);
"""

# 既存の DB に後から追加した列（テーブル, 列, 型）
MIGRATIONS = [
    ("events", "updated_at", "TEXT"),
    ("events", "gcal_event_id", "TEXT"),
    ("events", "gcal_hash", "TEXT"),
    ("notifications", "change_id", "INTEGER"),
]


def _t(value: Optional[str]) -> Optional[time]:
    return time.fromisoformat(value) if value else None


def _dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


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
        updated_at=_dt(row["updated_at"]),
        gcal_event_id=row["gcal_event_id"],
        gcal_hash=row["gcal_hash"],
    )


def _row_to_notification(row: sqlite3.Row) -> Notification:
    return Notification(
        id=row["id"],
        kind=row["kind"],
        message=row["message"],
        event_id=row["event_id"],
        change_id=row["change_id"],
        read=bool(row["read"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _row_to_change(row: sqlite3.Row) -> EventChange:
    return EventChange(
        id=row["id"],
        event_id=row["event_id"],
        kind=row["kind"],
        status=row["status"],
        reason=row["reason"],
        before=json.loads(row["before_json"]),
        after=json.loads(row["after_json"]),
        source=row["source"],
        source_message_id=row["source_message_id"],
        source_author=row["source_author"],
        source_channel=row["source_channel"],
        source_url=row["source_url"],
        raw_text=row["raw_text"],
        confidence=row["confidence"],
        created_at=datetime.fromisoformat(row["created_at"]),
        resolved_at=_dt(row["resolved_at"]),
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
        self._migrate()
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _migrate(self) -> None:
        """古いバージョンで作った DB に、足りない列を追加する。"""
        for table, column, ddl in MIGRATIONS:
            cols = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if cols and column not in cols:  # テーブルがまだない場合は SCHEMA が作る
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

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

    def update_event(self, ev: Event) -> Event:
        """予定の内容（タイトル・日時・場所・説明・状態）を書き換える。"""
        with self._lock:
            updated = self._now()
            self._conn.execute(
                """UPDATE events SET title = ?, date = ?, start_time = ?, end_time = ?, location = ?,
                   description = ?, status = ?, updated_at = ? WHERE id = ?""",
                (
                    ev.title,
                    ev.date.isoformat(),
                    ev.start_time.strftime("%H:%M") if ev.start_time else None,
                    ev.end_time.strftime("%H:%M") if ev.end_time else None,
                    ev.location,
                    ev.description,
                    ev.status,
                    updated,
                    ev.id,
                ),
            )
            self._conn.commit()
            ev.updated_at = datetime.fromisoformat(updated)
            return ev

    def delete_event(self, event_id: int) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT gcal_event_id FROM events WHERE id = ?", (event_id,)).fetchone()
            if row and row["gcal_event_id"]:  # Google カレンダーからも消す
                self._conn.execute(
                    "INSERT OR IGNORE INTO gcal_tombstones (gcal_event_id, created_at) VALUES (?, ?)",
                    (row["gcal_event_id"], self._now()),
                )
            cur = self._conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
            # この予定への確認待ちの変更は不要になる
            self._conn.execute(
                "UPDATE event_changes SET status = 'rejected', resolved_at = ? WHERE event_id = ? AND status = 'pending'",
                (self._now(), event_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    # ---------- changes (変更・取り消し) ----------
    def add_change(self, ch: EventChange) -> EventChange:
        with self._lock:
            created = self._now()
            cur = self._conn.execute(
                """INSERT INTO event_changes (event_id, kind, status, reason, before_json, after_json, source,
                   source_message_id, source_author, source_channel, source_url, raw_text, confidence, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    ch.event_id,
                    ch.kind,
                    ch.status,
                    ch.reason,
                    json.dumps(ch.before, ensure_ascii=False),
                    json.dumps(ch.after, ensure_ascii=False),
                    ch.source,
                    ch.source_message_id,
                    ch.source_author,
                    ch.source_channel,
                    ch.source_url,
                    ch.raw_text,
                    ch.confidence,
                    created,
                ),
            )
            self._conn.commit()
            ch.id = cur.lastrowid
            ch.created_at = datetime.fromisoformat(created)
            return ch

    def get_change(self, change_id: int) -> Optional[EventChange]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM event_changes WHERE id = ?", (change_id,)).fetchone()
        return _row_to_change(row) if row else None

    def list_changes(self, status: Optional[str] = None, event_id: Optional[int] = None) -> list[EventChange]:
        sql, params = "SELECT * FROM event_changes WHERE 1 = 1", []
        if status:
            sql += " AND status = ?"
            params.append(status)
        if event_id is not None:
            sql += " AND event_id = ?"
            params.append(event_id)
        sql += " ORDER BY id"
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_change(r) for r in rows]

    def resolve_change(self, ch: EventChange, status: str) -> EventChange:
        """変更の状態を applied / rejected / undone にする（反映時は before も保存し直す）。"""
        with self._lock:
            resolved = self._now()
            self._conn.execute(
                "UPDATE event_changes SET status = ?, before_json = ?, resolved_at = ? WHERE id = ?",
                (status, json.dumps(ch.before, ensure_ascii=False), resolved, ch.id),
            )
            self._conn.commit()
            ch.status = status
            ch.resolved_at = datetime.fromisoformat(resolved)
            return ch

    def latest_applied_change(self, event_id: int) -> Optional[EventChange]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM event_changes WHERE event_id = ? AND status = 'applied' ORDER BY id DESC LIMIT 1",
                (event_id,),
            ).fetchone()
        return _row_to_change(row) if row else None

    # ---------- notifications ----------
    def add_notification(self, n: Notification) -> Notification:
        with self._lock:
            created = self._now()
            cur = self._conn.execute(
                "INSERT INTO notifications (kind, message, event_id, change_id, read, created_at) VALUES (?,?,?,?,?,?)",
                (n.kind, n.message, n.event_id, n.change_id, int(n.read), created),
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

    # ---------- Google カレンダー ----------
    def set_gcal(self, event_id: int, gcal_event_id: Optional[str], gcal_hash: Optional[str]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE events SET gcal_event_id = ?, gcal_hash = ? WHERE id = ?", (gcal_event_id, gcal_hash, event_id)
            )
            self._conn.commit()

    def list_gcal_linked(self) -> list[Event]:
        """Google カレンダーに登録済みの予定（状態を問わない）。"""
        with self._lock:
            rows = self._conn.execute("SELECT * FROM events WHERE gcal_event_id IS NOT NULL ORDER BY id").fetchall()
        return [_row_to_event(r) for r in rows]

    def clear_gcal_links(self) -> None:
        """カレンダーを作り直すときなど、Google カレンダーとの対応をすべて忘れる。"""
        with self._lock:
            self._conn.execute("UPDATE events SET gcal_event_id = NULL, gcal_hash = NULL")
            self._conn.execute("DELETE FROM gcal_tombstones")
            self._conn.commit()

    def list_gcal_tombstones(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._conn.execute("SELECT gcal_event_id FROM gcal_tombstones").fetchall()]

    def remove_gcal_tombstone(self, gcal_event_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM gcal_tombstones WHERE gcal_event_id = ?", (gcal_event_id,))
            self._conn.commit()

    def get_state(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: Optional[str]) -> None:
        with self._lock:
            if value is None:
                self._conn.execute("DELETE FROM app_state WHERE key = ?", (key,))
            else:
                self._conn.execute(
                    "INSERT INTO app_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
            self._conn.commit()

    # ---------- 再精査キュー ----------
    def enqueue_message(self, msg: IncomingMessage, error: str, count_attempt: bool = False) -> None:
        """精査できなかったメッセージを保存する（同じメッセージは1件だけ）。"""
        with self._lock:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO message_queue (source, message_id, text, sent_at, author, channel, url,
                   attempts, last_error, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    msg.source,
                    msg.message_id,
                    msg.text,
                    msg.sent_at.isoformat(),
                    msg.author,
                    msg.channel,
                    msg.url,
                    int(count_attempt),
                    error[:500],
                    self._now(),
                ),
            )
            if cur.rowcount:
                for i, img in enumerate(msg.images):
                    self._conn.execute(
                        "INSERT INTO message_queue_images (queue_id, idx, mime, filename, data) VALUES (?,?,?,?,?)",
                        (cur.lastrowid, i, img.mime, img.filename, img.data),
                    )
            self._conn.commit()

    def list_queue(self, status: str = "waiting", limit: int = 50) -> list[tuple[int, IncomingMessage]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM message_queue WHERE status = ? ORDER BY id LIMIT ?", (status, limit)
            ).fetchall()
            items = []
            for r in rows:
                imgs = self._conn.execute(
                    "SELECT mime, filename, data FROM message_queue_images WHERE queue_id = ? ORDER BY idx", (r["id"],)
                ).fetchall()
                msg = IncomingMessage(
                    source=r["source"],
                    message_id=r["message_id"],
                    text=r["text"],
                    sent_at=datetime.fromisoformat(r["sent_at"]),
                    author=r["author"],
                    channel=r["channel"],
                    url=r["url"],
                    images=[ImageInput(data=bytes(i["data"]), mime=i["mime"], filename=i["filename"]) for i in imgs],
                )
                items.append((r["id"], msg))
        return items

    def queue_failed(self, queue_id: int, error: str, max_attempts: Optional[int]) -> None:
        """再精査に失敗した。max_attempts が None（接続できなかった）なら回数に数えない。"""
        with self._lock:
            if max_attempts is None:
                self._conn.execute("UPDATE message_queue SET last_error = ? WHERE id = ?", (error[:500], queue_id))
            else:
                self._conn.execute(
                    """UPDATE message_queue SET attempts = attempts + 1, last_error = ?,
                       status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE status END WHERE id = ?""",
                    (error[:500], max_attempts, queue_id),
                )
            self._conn.commit()

    def dequeue(self, queue_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM message_queue_images WHERE queue_id = ?", (queue_id,))
            self._conn.execute("DELETE FROM message_queue WHERE id = ?", (queue_id,))
            self._conn.commit()

    def queue_counts(self) -> dict:
        with self._lock:
            rows = self._conn.execute("SELECT status, COUNT(*) FROM message_queue GROUP BY status").fetchall()
        counts = {"waiting": 0, "failed": 0}
        counts.update({r[0]: r[1] for r in rows})
        return counts

    def retry_failed(self) -> int:
        with self._lock:
            cur = self._conn.execute("UPDATE message_queue SET status = 'waiting', attempts = 0 WHERE status = 'failed'")
            self._conn.commit()
            return cur.rowcount

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
