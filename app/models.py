"""ドメインモデル。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Optional

from pydantic import BaseModel, Field


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

    title: str = ""
    date: str = ""  # YYYY-MM-DD
    start_time: str = ""  # HH:MM or ""
    end_time: str = ""  # HH:MM or ""
    location: str = ""
    description: str = ""
    confidence: float = 0.0


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
                    "title": {"type": "string"},
                    "date": {"type": "string"},
                    "start_time": {"type": "string"},
                    "end_time": {"type": "string"},
                    "location": {"type": "string"},
                    "description": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["title", "date", "start_time", "end_time", "location", "description", "confidence"],
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
    status: str = "confirmed"  # confirmed / pending / rejected
    source: str = "manual"
    source_message_id: str = ""
    source_author: str = ""
    source_channel: str = ""
    source_url: str = ""
    raw_text: str = ""
    confidence: float = 1.0
    id: Optional[int] = None
    created_at: Optional[datetime] = None

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
            "label": format_event_line(self),
            "time_range": format_time_range(self),
        }


@dataclass
class Notification:
    kind: str  # added / conflict / resolved / info
    message: str
    event_id: Optional[int] = None
    read: bool = False
    id: Optional[int] = None
    created_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "message": self.message,
            "event_id": self.event_id,
            "read": self.read,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
