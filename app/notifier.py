"""通知の配信。Web UI(DB) に必ず記録し、登録された各シンク(Discord DM等)にも送る。"""
from __future__ import annotations

import logging
from typing import Protocol

from .db import Database
from datetime import date

from .formatting import (
    format_added_message,
    format_cancelled_message,
    format_change_pending_message,
    format_conflict_message,
    format_event_line,
    format_merged_message,
    format_updated_message,
)
from .models import Event, EventChange, Notification, with_fields

log = logging.getLogger(__name__)


class NotificationSink(Protocol):
    """通知先。Slack 等を追加する場合もこのインターフェースを実装する。"""

    name: str

    async def notify_added(self, event: Event, message: str) -> None: ...

    async def notify_conflict(self, event: Event, conflicts: list[Event], message: str) -> None: ...

    # 以下は任意（実装していないシンクには送らない）
    # async def notify_changed(self, change: EventChange, event: Event, message: str) -> None: ...
    # async def notify_change_pending(self, change: EventChange, event: Event, message: str) -> None: ...


def _before_event(change: EventChange, current: Event | None) -> Event:
    """変更履歴の before から、変更前の予定を組み立てる。"""
    base = current or Event(title="", date=date.fromisoformat(change.before["date"]), id=change.event_id)
    return with_fields(base, change.before)


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

    async def conflict(self, event: Event, conflicts: list[Event], likely_same: bool = False) -> Notification:
        message = format_conflict_message(event, conflicts, likely_same)
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

    # ---------- 変更・取り消し ----------
    async def _each(self, method: str, *args) -> None:
        for sink in self.sinks:
            fn = getattr(sink, method, None)
            if fn is None:
                continue
            try:
                await fn(*args)
            except Exception:
                log.exception("通知の送信に失敗しました (%s.%s)", sink.name, method)

    async def changed(self, change: EventChange, event: Event) -> Notification:
        """自動で、または承認されて反映された変更・取り消し・まとめ。"""
        before = _before_event(change, event)
        if change.kind == "cancel":
            kind, message = "cancelled", format_cancelled_message(before)
        elif change.kind == "merge":
            kind, message = "merged", format_merged_message(before, event)
        else:
            kind, message = "updated", format_updated_message(before, event)
        n = self.db.add_notification(Notification(kind=kind, message=message, event_id=event.id, change_id=change.id))
        await self._each("notify_changed", change, event, message)
        return n

    async def change_pending(self, change: EventChange, event: Event, conflicts: list[Event]) -> Notification:
        """あいまいな変更・取り消しの確認通知。"""
        after = with_fields(event, {k: v for k, v in change.after.items() if k != "status"}) if change.after else None
        message = format_change_pending_message(change.kind, event, after, conflicts, change.reason)
        n = self.db.add_notification(
            Notification(kind="change_pending", message=message, event_id=event.id, change_id=change.id)
        )
        await self._each("notify_change_pending", change, event, message)
        return n

    async def change_rejected(self, change: EventChange) -> Notification:
        before = _before_event(change, self.db.get_event(change.event_id))
        verb = "取り消しませんでした" if change.kind == "cancel" else "変更しませんでした"
        message = f"{verb}: {format_event_line(before)}"
        return self.db.add_notification(
            Notification(kind="resolved", message=message, event_id=change.event_id, change_id=change.id)
        )

    async def merged_into(self, event: Event) -> Notification:
        """確認待ちの予定を、新しい情報なしで既存の予定にまとめたとき（Web UI のみ）。"""
        message = f"同じ予定としてまとめました: {format_event_line(event)}"
        return self.db.add_notification(Notification(kind="resolved", message=message, event_id=event.id))

    async def undone(self, change: EventChange, event: Event) -> Notification:
        message = f"元に戻しました: {format_event_line(event)}"
        return self.db.add_notification(
            Notification(kind="undone", message=message, event_id=event.id, change_id=change.id)
        )
