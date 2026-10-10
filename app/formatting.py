"""通知文などの日本語フォーマット。"""
from __future__ import annotations

from datetime import date, time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Event

WEEKDAYS_JA = "月火水木金土日"


def format_date_ja(d: date) -> str:
    """2026-10-15 -> 10月15日(木)"""
    return f"{d.month}月{d.day}日({WEEKDAYS_JA[d.weekday()]})"


def format_hour_ja(t: time) -> str:
    """14:00 -> 14時 / 14:30 -> 14時30分"""
    return f"{t.hour}時" if t.minute == 0 else f"{t.hour}時{t.minute}分"


def format_event_line(event: "Event") -> str:
    """通知の本文。仕様: ○月○日(○)○時から○○（内容）

    例: 10月15日(木)14時から定例ミーティング
        10月20日(火) 終日 レポート提出締切
    """
    head = format_date_ja(event.date)
    if event.start_time is None:
        return f"{head} 終日 {event.title}"
    return f"{head}{format_hour_ja(event.start_time)}から{event.title}"


def format_time_range(event: "Event") -> str:
    if event.start_time is None:
        return "終日"
    s = event.start_time.strftime("%H:%M")
    if event.end_time:
        return f"{s}–{event.end_time.strftime('%H:%M')}"
    return s


def format_added_message(event: "Event") -> str:
    return format_event_line(event)


def format_when_ja(event: "Event") -> str:
    """10月15日(木)14時 / 10月20日(火) 終日"""
    head = format_date_ja(event.date)
    return f"{head} 終日" if event.start_time is None else f"{head}{format_hour_ja(event.start_time)}"


def _hm(t: "time | None") -> str:
    return t.strftime("%H:%M") if t else "なし"


def _change_details(before: "Event", after: "Event") -> list[str]:
    details = []
    if before.date != after.date or before.start_time != after.start_time:
        details.append(f"変更前: {format_when_ja(before)}")
    elif before.end_time != after.end_time:
        details.append(f"終了: {_hm(before.end_time)} → {_hm(after.end_time)}")
    if before.location != after.location:
        details.append(f"場所: {before.location or 'なし'} → {after.location or 'なし'}")
    return details


def format_updated_message(before: "Event", after: "Event") -> str:
    """例: 10月16日(金)15時から定例ミーティングに変更しました（変更前: 10月15日(木)14時）"""
    details = _change_details(before, after)
    return f"{format_event_line(after)}に変更しました" + (f"（{'、'.join(details)}）" if details else "")


def format_cancelled_message(event: "Event") -> str:
    """例: 10月15日(木)14時から定例ミーティングを取り消しました"""
    return f"{format_event_line(event)}を取り消しました"


def format_change_pending_message(
    kind: str, before: "Event", after: "Event | None", conflicts: list["Event"], reason: str = ""
) -> str:
    if kind == "cancel":
        lines = [f"{before.title}の予定を取り消しますか？", f"対象: {format_event_line(before)}"]
    else:
        lines = [
            f"{before.title}の予定を変更しますか？",
            f"変更前: {format_event_line(before)}",
            f"変更後: {format_event_line(after)}",
        ]
        if after is not None and before.location != after.location:
            lines.append(f"場所: {before.location or 'なし'} → {after.location or 'なし'}")
        for c in conflicts:
            lines.append(f"重なる予定: {format_event_line(c)}")
    if reason:
        lines.append(f"（{reason}）")
    return "\n".join(lines)


def format_conflict_message(event: "Event", conflicts: list["Event"]) -> str:
    lines = [
        f"{format_date_ja(event.date)}は既に予定があります。追加しますか？",
        f"新しい予定: {format_event_line(event)}",
    ]
    for c in conflicts:
        lines.append(f"既存の予定: {format_event_line(c)}")
    return "\n".join(lines)
