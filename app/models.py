"""ドメインモデル。"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
from typing import Optional

from pydantic import BaseModel, Field, field_validator


@dataclass
class ImageInput:
    data: bytes
    mime: str = "image/png"
    filename: str = ""


@dataclass
class IncomingMessage:
    """各連絡ツール(Discord/Slack/Zoom...)から受け取ったメッセージの共通形式。"""

    source: str  # "discord" / "slack" / "manual" ...
    message_id: str
    text: str
    sent_at: datetime  # tz-aware
    author: str = ""
    channel: str = ""
    url: str = ""
    images: list[ImageInput] = field(default_factory=list)


class ExtractedEvent(BaseModel):
    """LLMが返す1件分の予定（検証前）。"""

    # add: 新しい予定 / update: 登録済みの予定の変更 / cancel: 登録済みの予定の取り消し
    action: str = "add"
    target_id: int = 0  # update / cancel の対象（【登録済みの予定】の #番号）。不明なら 0
    target_title: str = ""  # 対象の予定の元のタイトル（target_id が不明なとき用）
    target_date: str = ""  # 対象の予定の元の日付 YYYY-MM-DD（同上）
    title: str = ""
    date: str = ""  # YYYY-MM-DD
    start_time: str = ""  # HH:MM or ""
    end_time: str = ""  # HH:MM or ""
    location: str = ""
    description: str = ""
    confidence: float = 0.0

    @field_validator("target_id", mode="before")
    @classmethod
    def _lenient_id(cls, value):
        """"#12" や "12" や null が返ってきても受け付ける。"""
        if isinstance(value, str):
            digits = value.strip().lstrip("#＃").strip()
            return int(digits) if digits.isdigit() else 0
        return value or 0

    @field_validator("action", mode="before")
    @classmethod
    def _lenient_action(cls, value):
        value = str(value or "add").strip().lower()
        aliases = {"delete": "cancel", "remove": "cancel", "change": "update", "modify": "update", "reschedule": "update"}
        value = aliases.get(value, value)
        return value if value in ("add", "update", "cancel") else "add"


class ExtractionResult(BaseModel):
    events: list[ExtractedEvent] = Field(default_factory=list)


# Ollama の構造化出力 (format) に渡す JSON Schema
EXTRACTION_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["add", "update", "cancel"]},
                    "target_id": {"type": "integer"},
                    "target_title": {"type": "string"},
                    "target_date": {"type": "string"},
                    "title": {"type": "string"},
                    "date": {"type": "string"},
                    "start_time": {"type": "string"},
                    "end_time": {"type": "string"},
                    "location": {"type": "string"},
                    "description": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": [
                    "action", "target_id", "target_title", "target_date",
                    "title", "date", "start_time", "end_time", "location", "description", "confidence",
                ],
            },
        }
    },
    "required": ["events"],
}


@dataclass
class Event:
    title: str
    date: date
    start_time: Optional[time] = None
    end_time: Optional[time] = None
    location: str = ""
    description: str = ""
    status: str = "confirmed"  # confirmed / pending / rejected / cancelled
    source: str = "manual"
    source_message_id: str = ""
    source_author: str = ""
    source_channel: str = ""
    source_url: str = ""
    raw_text: str = ""
    confidence: float = 1.0
    id: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    @property
    def all_day(self) -> bool:
        return self.start_time is None

    def to_dict(self) -> dict:
        from .formatting import format_event_line, format_time_range

        return {
            "id": self.id,
            "title": self.title,
            "date": self.date.isoformat(),
            "start_time": self.start_time.strftime("%H:%M") if self.start_time else None,
            "end_time": self.end_time.strftime("%H:%M") if self.end_time else None,
            "all_day": self.all_day,
            "location": self.location,
            "description": self.description,
            "status": self.status,
            "source": self.source,
            "source_author": self.source_author,
            "source_channel": self.source_channel,
            "source_url": self.source_url,
            "raw_text": self.raw_text,
            "confidence": self.confidence,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "label": format_event_line(self),
            "time_range": format_time_range(self),
        }


# 変更・取り消しで書き換わる項目（変更履歴のスナップショットに使う）
EDITABLE_FIELDS = ("title", "date", "start_time", "end_time", "location", "description", "status")


def event_fields(ev: Event) -> dict:
    """予定の書き換わる項目を JSON にできる形で取り出す。"""
    return {
        "title": ev.title,
        "date": ev.date.isoformat(),
        "start_time": ev.start_time.strftime("%H:%M") if ev.start_time else None,
        "end_time": ev.end_time.strftime("%H:%M") if ev.end_time else None,
        "location": ev.location,
        "description": ev.description,
        "status": ev.status,
    }


def with_fields(ev: Event, fields: dict) -> Event:
    """event_fields() の形の値を当てはめた新しい Event を返す（元の Event は変更しない）。"""
    values = {}
    for key in EDITABLE_FIELDS:
        if key not in fields:
            continue
        value = fields[key]
        if key == "date":
            value = date.fromisoformat(value)
        elif key in ("start_time", "end_time"):
            value = time.fromisoformat(value) if value else None
        values[key] = value
    return replace(ev, **values)


@dataclass
class EventChange:
    """メッセージから見つかった、登録済みの予定への変更・取り消し。"""

    event_id: int
    kind: str  # update / cancel
    before: dict  # event_fields() の形（反映前）
    after: dict = field(default_factory=dict)  # event_fields() の形（update のみ）
    status: str = "pending"  # pending / applied / rejected / undone
    reason: str = ""  # 確認待ちにした理由
    source: str = "manual"
    source_message_id: str = ""
    source_author: str = ""
    source_channel: str = ""
    source_url: str = ""
    raw_text: str = ""
    confidence: float = 1.0
    id: Optional[int] = None
    created_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "event_id": self.event_id,
            "kind": self.kind,
            "status": self.status,
            "reason": self.reason,
            "before": self.before,
            "after": self.after,
            "source": self.source,
            "source_author": self.source_author,
            "source_channel": self.source_channel,
            "source_url": self.source_url,
            "raw_text": self.raw_text,
            "confidence": self.confidence,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
        }


@dataclass
class Notification:
    kind: str  # added / conflict / resolved / info / updated / cancelled / change_pending / undone
    message: str
    event_id: Optional[int] = None
    change_id: Optional[int] = None
    read: bool = False
    id: Optional[int] = None
    created_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "message": self.message,
            "event_id": self.event_id,
            "change_id": self.change_id,
            "read": self.read,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
