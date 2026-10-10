"""メッセージ精査 → 予定登録 → 重複判定 → 通知 のパイプライン。"""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Optional, Protocol

from .config import Settings
from .db import Database
from .models import Event, ExtractedEvent, ExtractionResult, ImageInput, IncomingMessage
from .notifier import Notifier

log = logging.getLogger(__name__)

# 日付・時刻・予定らしき表現（これがなく画像もないメッセージはLLMに送らない）
_DATE_HINT = re.compile(
    r"[0-9０-９]|[一二三四五六七八九十]+日|今日|本日|明日|あした|あす|明後日|あさって|今週|来週|再来週|今月|来月|週末|"
    r"[月火水木金土日]曜|午前|午後|正午|朝|昼|夕方|夜|日程|予定|締切|締め切り|〆切|期限|"
    r"ミーティング|MTG|mtg|会議|打ち合わせ|打合せ|面談|面接|集合|イベント|飲み会|授業|講義|試験|テスト"
)


class Extractor(Protocol):
    async def extract(
        self,
        text: str,
        sent_at: datetime,
        author: str = "",
        channel: str = "",
        images: Optional[list[ImageInput]] = None,
    ) -> ExtractionResult: ...


@dataclass
class EventResult:
    status: str  # added / pending / duplicate
    event: Event
    conflicts: list[Event] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "event": self.event.to_dict(),
            "conflicts": [c.to_dict() for c in self.conflicts],
        }


@dataclass
class AnalysisOutcome:
    results: list[EventResult] = field(default_factory=list)
    skipped: str = ""  # スキップ理由（あれば）

    def to_dict(self) -> dict:
        return {"skipped": self.skipped, "results": [r.to_dict() for r in self.results]}


def looks_like_schedule(text: str) -> bool:
    return bool(_DATE_HINT.search(text or ""))


def _parse_time(value: str) -> Optional[time]:
    value = (value or "").strip().translate(str.maketrans("０１２３４５６７８９：", "0123456789:"))
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", value)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h == 24 and mi == 0:
        return time(23, 59)
    if 0 <= h <= 23 and 0 <= mi <= 59:
        return time(h, mi)
    return None


def _normalize_title(title: str) -> str:
    return re.sub(r"[\s　・、。,.!！?？「」『』()（）【】\-ー_~〜]", "", title).lower()


def _similar(a: str, b: str) -> bool:
    na, nb = _normalize_title(a), _normalize_title(b)
    return bool(na and nb) and (na == nb or na in nb or nb in na)


class ScheduleService:
    def __init__(self, settings: Settings, db: Database, extractor: Extractor, notifier: Notifier):
        self.settings = settings
        self.db = db
        self.extractor = extractor
        self.notifier = notifier
        self._save_lock = asyncio.Lock()

    def today(self) -> date:
        return datetime.now(self.settings.tz).date()

    # ---------- 精査 ----------
    def validate(self, raw: ExtractedEvent, msg: IncomingMessage) -> Optional[Event]:
        title = (raw.title or "").strip()[:60]
        if not title:
            return None
        if raw.confidence < self.settings.min_confidence:
            log.info("確信度が低いためスキップ: %s (%.2f)", title, raw.confidence)
            return None
        try:
            d = date.fromisoformat(raw.date.strip())
        except (ValueError, AttributeError):
            log.info("日付を解釈できないためスキップ: %s %r", title, raw.date)
            return None
        today = self.today()
        if d < today or d > today + timedelta(days=730):
            log.info("範囲外の日付のためスキップ: %s %s", title, d)
            return None
        start = _parse_time(raw.start_time)
        end = _parse_time(raw.end_time) if start else None
        if end and end <= start:
            end = None
        return Event(
            title=title,
            date=d,
            start_time=start,
            end_time=end,
            location=(raw.location or "").strip()[:200],
            description=(raw.description or "").strip()[:500],
            source=msg.source,
            source_message_id=msg.message_id,
            source_author=msg.author,
            source_channel=msg.channel,
            source_url=msg.url,
            raw_text=msg.text[:2000],
            confidence=max(0.0, min(1.0, raw.confidence)),
        )

    async def handle_message(self, msg: IncomingMessage, force: bool = False) -> AnalysisOutcome:
        if not force and not self.db.mark_processed(msg.source, msg.message_id):
            return AnalysisOutcome(skipped="処理済みのメッセージです")
        if not msg.text.strip() and not msg.images:
            return AnalysisOutcome(skipped="本文も画像もありません")
        if self.settings.prefilter and not msg.images and not looks_like_schedule(msg.text):
            return AnalysisOutcome(skipped="日付や予定に関する表現が見つかりませんでした")

        try:
            result = await self.extractor.extract(
                text=msg.text,
                sent_at=msg.sent_at.astimezone(self.settings.tz),
                author=msg.author,
                channel=msg.channel,
                images=msg.images,
            )
        except Exception:
            # LLM が使えなかった場合は、同じメッセージを再送・転送すれば再処理できるようにする
            self.db.unmark_processed(msg.source, msg.message_id)
            raise
        outcome = AnalysisOutcome()
        for raw in result.events:
            event = self.validate(raw, msg)
            if event:
                outcome.results.append(await self.add_event(event))
        if not outcome.results:
            outcome.skipped = "予定は見つかりませんでした"
        return outcome

    # ---------- 登録・重複判定 ----------
    def _interval(self, ev: Event) -> tuple[datetime, datetime]:
        start = datetime.combine(ev.date, ev.start_time)
        if ev.end_time and ev.end_time > ev.start_time:
            end = datetime.combine(ev.date, ev.end_time)
        else:
            end = start + timedelta(minutes=self.settings.default_duration_min)
        return start, end

    def overlaps(self, a: Event, b: Event) -> bool:
        if a.date != b.date:
            return False
        if self.settings.conflict_mode == "same_day":
            return True
        if a.start_time is None or b.start_time is None:
            return True  # 終日予定は同日の予定すべてと被るとみなす
        a_start, a_end = self._interval(a)
        b_start, b_end = self._interval(b)
        return a_start < b_end and b_start < a_end

    def find_conflicts(self, event: Event) -> list[Event]:
        same_day = self.db.list_events(statuses=("confirmed", "pending"), on_date=event.date)
        return [e for e in same_day if e.id != event.id and self.overlaps(event, e)]

    def find_duplicate(self, event: Event) -> Optional[Event]:
        same_day = self.db.list_events(statuses=("confirmed", "pending"), on_date=event.date)
        for e in same_day:
            if e.start_time == event.start_time and _similar(e.title, event.title):
                return e
        return None

    async def add_event(self, event: Event) -> EventResult:
        async with self._save_lock:
            dup = self.find_duplicate(event)
            if dup:
                log.info("既に登録済みの予定のためスキップ: %s", dup.title)
                return EventResult(status="duplicate", event=dup)
            conflicts = self.find_conflicts(event)
            event.status = "pending" if conflicts else "confirmed"
            self.db.add_event(event)

        if conflicts:
            await self.notifier.conflict(event, conflicts)
            return EventResult(status="pending", event=event, conflicts=conflicts)
        await self.notifier.added(event)
        return EventResult(status="added", event=event)

    async def confirm(self, event_id: int) -> Event:
        event = self.db.get_event(event_id)
        if event is None:
            raise KeyError(event_id)
        if event.status != "pending":
            return event
        self.db.set_status(event_id, "confirmed")
        event.status = "confirmed"
        await self.notifier.added(event)
        return event

    async def reject(self, event_id: int) -> Event:
        event = self.db.get_event(event_id)
        if event is None:
            raise KeyError(event_id)
        if event.status != "pending":
            return event
        self.db.set_status(event_id, "rejected")
        event.status = "rejected"
        await self.notifier.rejected(event)
        return event

    def pending_with_conflicts(self) -> list[dict]:
        items = []
        for ev in self.db.list_events(statuses=("pending",)):
            others = [c for c in self.find_conflicts(ev)]
            items.append({"event": ev.to_dict(), "conflicts": [c.to_dict() for c in others]})
        return items
