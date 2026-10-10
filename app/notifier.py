"""通知の配信。Web UI(DB) に必ず記録し、登録された各シンク(Discord DM等)にも送る。"""
from __future__ import annotations

import logging
from typing import Protocol

from .db import Database
from .formatting import format_added_message, format_conflict_message, format_event_line
from .models import Event, Notification

log = logging.getLogger(__name__)


class NotificationSink(Protocol):
    """通知先。Slack 等を追加する場合もこのインターフェースを実装する。"""

    name: str

    async def notify_added(self, event: Event, message: str) -> None: ...

    async def notify_conflict(self, event: Event, conflicts: list[Event], message: str) -> None: ...


class Notifier:
    def __init__(self, db: Database):
        self.db = db
        self.sinks: list[NotificationSink] = []

    def register(self, sink: NotificationSink) -> None:
        self.sinks.append(sink)

    async def added(self, event: Event) -> Notification:
        message = format_added_message(event)
        n = self.db.add_notification(Notification(kind="added", message=message, event_id=event.id))
        for sink in self.sinks:
            try:
                await sink.notify_added(event, message)
            except Exception:
                log.exception("通知の送信に失敗しました (%s)", sink.name)
        return n

    async def conflict(self, event: Event, conflicts: list[Event]) -> Notification:
        message = format_conflict_message(event, conflicts)
        n = self.db.add_notification(Notification(kind="conflict", message=message, event_id=event.id))
        for sink in self.sinks:
            try:
                await sink.notify_conflict(event, conflicts, message)
            except Exception:
                log.exception("確認通知の送信に失敗しました (%s)", sink.name)
        return n

    async def rejected(self, event: Event) -> Notification:
        message = f"追加しませんでした: {format_event_line(event)}"
        return self.db.add_notification(Notification(kind="resolved", message=message, event_id=event.id))
