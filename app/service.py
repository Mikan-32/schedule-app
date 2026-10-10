"""メッセージ精査 → 予定登録 → 重複判定 → 通知 のパイプライン。"""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Optional, Protocol

from .config import Settings
from .db import Database
from .models import (
    Event,
    EventChange,
    ExtractedEvent,
    ExtractionResult,
    ImageInput,
    IncomingMessage,
    event_fields,
    with_fields,
)
from .notifier import Notifier

log = logging.getLogger(__name__)

# 日付・時刻・予定らしき表現（これがなく画像もないメッセージはLLMに送らない）
_DATE_HINT = re.compile(
    r"[0-9０-９]|[一二三四五六七八九十]+日|今日|本日|明日|あした|あす|明後日|あさって|今週|来週|再来週|今月|来月|週末|"
    r"[月火水木金土日]曜|午前|午後|正午|朝|昼|夕方|夜|日程|予定|締切|締め切り|〆切|期限|"
    r"ミーティング|MTG|mtg|会議|打ち合わせ|打合せ|面談|面接|集合|イベント|飲み会|授業|講義|試験|テスト|"
    r"中止|延期|キャンセル|取り消|取消|取りやめ|取り止め|変更|リスケ|順延|前倒し|なくなり|無くなり|休講"
)

# 変更・取り消しの対象を探すとき、LLM に渡す登録済みの予定の範囲
CANDIDATE_DAYS = 180
CANDIDATE_LIMIT = 50


class Extractor(Protocol):
    async def extract(
        self,
        text: str,
        sent_at: datetime,
        author: str = "",
        channel: str = "",
        images: Optional[list[ImageInput]] = None,
        existing: Optional[list[Event]] = None,
    ) -> ExtractionResult: ...


@dataclass
class EventResult:
    # added / pending / duplicate / merged / updated / cancelled / change_pending
    status: str
    event: Event
    conflicts: list[Event] = field(default_factory=list)
    change: Optional[EventChange] = None

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "event": self.event.to_dict(),
            "conflicts": [c.to_dict() for c in self.conflicts],
            "change": self.change.to_dict() if self.change else None,
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


def _parse_date(value: str) -> Optional[date]:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


# タイトルの表記ゆれをそろえる（同じ予定かどうかの判定用）
_TITLE_SYNONYMS = [
    (re.compile(r"ミーティング|ミーテイング|meeting|mtg|打ち合わせ|打ち合せ|打合わせ|打合せ"), "会議"),
    (re.compile(r"締め切り|締切り|〆切り|〆切|しめきり|deadline"), "締切"),
    (re.compile(r"リマインダー|リマインド|reminder|再掲|再送"), ""),
]
SIMILAR_THRESHOLD = 0.6


def _normalize_title(title: str) -> str:
    s = unicodedata.normalize("NFKC", title or "").lower()  # 全角英数・半角カナをそろえる
    for pattern, repl in _TITLE_SYNONYMS:
        s = pattern.sub(repl, s)
    return re.sub(r"[\s・、。,.!?「」『』()\[\]【】\-ー_~〜:;/]", "", s)


def _bigrams(s: str) -> set[str]:
    return {s[i : i + 2] for i in range(len(s) - 1)} if len(s) > 1 else {s}


def _title_score(a: str, b: str) -> float:
    """タイトルの近さ (0〜1)。表記ゆれをそろえたうえで、一致・包含・文字の重なりで測る。"""
    na, nb = _normalize_title(a), _normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if min(len(na), len(nb)) >= 2 and (na in nb or nb in na):
        return 0.9
    ba, bb = _bigrams(na), _bigrams(nb)
    return 2 * len(ba & bb) / (len(ba) + len(bb))


def _similar(a: str, b: str) -> bool:
    return _title_score(a, b) >= SIMILAR_THRESHOLD


def _times_compatible(a: Event, b: Event) -> bool:
    """開始時刻が同じか、どちらかが時刻なし（終日・時刻未定）なら同じ予定でありうる。"""
    return a.start_time is None or b.start_time is None or a.start_time == b.start_time


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

        candidates = self.change_candidates()
        try:
            result = await self.extractor.extract(
                text=msg.text,
                sent_at=msg.sent_at.astimezone(self.settings.tz),
                author=msg.author,
                channel=msg.channel,
                images=msg.images,
                existing=candidates,
            )
        except Exception:
            # LLM が使えなかった場合は、同じメッセージを再送・転送すれば再処理できるようにする
            self.db.unmark_processed(msg.source, msg.message_id)
            raise
        outcome = AnalysisOutcome()
        notes: list[str] = []
        for raw in result.events:
            if raw.action in ("update", "cancel"):
                res, note = await self.handle_change(raw, msg, candidates)
                if note:
                    notes.append(note)
            elif raw.action == "same":
                res = await self.handle_same(raw, msg, candidates)
            else:
                event = self.validate(raw, msg)
                res = await self.add_event(event) if event else None
            if res:
                outcome.results.append(res)
        if not outcome.results:
            outcome.skipped = notes[0] if notes else "予定は見つかりませんでした"
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

    def find_same(self, event: Event) -> Optional[Event]:
        """登録済みの同じ予定（同じ日・開始時刻が矛盾しない・タイトルが似ている）を探す。"""
        best, best_key = None, None
        for e in self.db.list_events(statuses=("confirmed", "pending"), on_date=event.date):
            if e.id == event.id or not _times_compatible(e, event):
                continue
            score = _title_score(e.title, event.title)
            if score < SIMILAR_THRESHOLD:
                continue
            key = (e.start_time == event.start_time, score)
            if best_key is None or key > best_key:
                best, best_key = e, key
        return best

    def merge_candidate(self, event: Event, conflicts: list[Event]) -> Optional[Event]:
        """重なっている予定のうち、同じ予定である可能性がいちばん高いもの（まとめる先の候補）。"""
        if not conflicts:
            return None
        return max(conflicts, key=lambda c: (c.start_time == event.start_time, _title_score(c.title, event.title)))

    def _sorted_conflicts(self, event: Event, conflicts: list[Event]) -> tuple[list[Event], bool]:
        """まとめる先の候補を先頭にした重なる予定と、「同じ予定かもしれない」か。"""
        cand = self.merge_candidate(event, conflicts)
        if cand is None:
            return conflicts, False
        return [cand] + [c for c in conflicts if c.id != cand.id], cand.start_time == event.start_time

    @staticmethod
    def _merged(target: Event, incoming: Event) -> Optional[Event]:
        """target に、incoming の情報のうち足りないものだけを補った予定。新しい情報がなければ None。"""
        fill: dict = {}
        if target.start_time is None and incoming.start_time is not None:
            fill["start_time"], fill["end_time"] = incoming.start_time, incoming.end_time
        elif (
            target.start_time is not None
            and target.end_time is None
            and incoming.end_time is not None
            and incoming.start_time in (None, target.start_time)
            and incoming.end_time > target.start_time
        ):
            fill["end_time"] = incoming.end_time
        if not target.location and incoming.location:
            fill["location"] = incoming.location
        if not target.description and incoming.description:
            fill["description"] = incoming.description
        return replace(target, **fill) if fill else None

    def _merge_locked(self, target: Event, incoming: Event) -> tuple[Event, Optional[EventChange]]:
        """同じ予定の連絡をまとめる（_save_lock 内で呼ぶ）。足りない情報があれば補い、変更履歴に残す。"""
        merged = self._merged(target, incoming)
        if merged is None:
            return target, None
        change = EventChange(
            event_id=target.id,
            kind="merge",
            before=event_fields(target),
            after=event_fields(merged),
            source=incoming.source,
            source_message_id=incoming.source_message_id,
            source_author=incoming.source_author,
            source_channel=incoming.source_channel,
            source_url=incoming.source_url,
            raw_text=incoming.raw_text,
            confidence=incoming.confidence,
        )
        self.db.add_change(change)
        return self._apply_locked(change, target), change

    async def add_event(self, event: Event, same_as_id: Optional[int] = None) -> EventResult:
        """予定を登録する。同じ予定が既にあればまとめ、時間が重なる予定があれば確認待ちにする。"""
        change: Optional[EventChange] = None
        async with self._save_lock:
            same = self.db.get_event(same_as_id) if same_as_id else None
            if same is None or same.status not in ("confirmed", "pending"):
                same = self.find_same(event)
            if same:
                merged, change = self._merge_locked(same, event)
            else:
                conflicts, likely_same = self._sorted_conflicts(event, self.find_conflicts(event))
                event.status = "pending" if conflicts else "confirmed"
                self.db.add_event(event)

        if same:
            if change is None:
                log.info("既に登録済みの予定のためスキップ: %s", same.title)
                return EventResult(status="duplicate", event=same)
            await self.notifier.changed(change, merged)
            return EventResult(status="merged", event=merged, change=change)
        if conflicts:
            await self.notifier.conflict(event, conflicts, likely_same)
            return EventResult(status="pending", event=event, conflicts=conflicts)
        await self.notifier.added(event)
        return EventResult(status="added", event=event)

    async def handle_same(self, raw: ExtractedEvent, msg: IncomingMessage, candidates: list[Event]) -> Optional[EventResult]:
        """LLM が「登録済みの予定と同じ」と判断した連絡。日時が矛盾しなければその予定にまとめる。"""
        target = {e.id: e for e in candidates}.get(raw.target_id) if raw.target_id else None
        if target is not None and not (raw.date or "").strip():
            raw = raw.model_copy(update={"date": target.date.isoformat()})
        event = self.validate(raw, msg)
        if event is None:
            return None
        if target is not None and event.date == target.date and _times_compatible(target, event):
            return await self.add_event(event, same_as_id=target.id)
        # LLM の判断と日時が食い違う → 新しい予定として扱う（似た予定があればそこで照合される）
        return await self.add_event(event)

    async def merge_pending(self, event_id: int, into_id: Optional[int] = None) -> Event:
        """確認待ちの予定を「同じ予定」として既存の予定にまとめる。"""
        async with self._save_lock:
            ev = self.db.get_event(event_id)
            if ev is None:
                raise KeyError(event_id)
            if ev.status != "pending":
                raise ValueError("この予定は既に処理されています")
            if into_id is not None:
                target = self.db.get_event(into_id)
            else:
                target = self.merge_candidate(ev, self.find_conflicts(ev))
            if target is None or target.id == ev.id or target.status not in ("confirmed", "pending"):
                raise ValueError("まとめる先の予定が見つかりません")
            merged, change = self._merge_locked(target, ev)
            self.db.set_status(ev.id, "merged")
        if change:
            await self.notifier.changed(change, merged)
        else:
            await self.notifier.merged_into(merged)
        return merged

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
        """確認待ちの一覧（重複した新しい予定 + あいまいな変更・取り消し）。"""
        items = []
        for ev in self.db.list_events(statuses=("pending",)):
            others, likely_same = self._sorted_conflicts(ev, self.find_conflicts(ev))
            items.append(
                {
                    "kind": "add",
                    "event": ev.to_dict(),
                    "conflicts": [c.to_dict() for c in others],
                    "merge_into": others[0].id if others else None,  # 「同じ予定としてまとめる」の行き先
                    "likely_same": likely_same,
                }
            )
        for ch in self.db.list_changes(status="pending"):
            target = self.db.get_event(ch.event_id)
            if target is None or target.status in ("cancelled", "rejected"):
                continue  # 対象がもうない（承認しようとしても反映されない）
            item = {"kind": ch.kind, "change": ch.to_dict(), "event": target.to_dict(), "conflicts": []}
            if ch.kind == "update":
                after = self._updated(target, ch)
                item["after"] = after.to_dict()
                item["conflicts"] = [c.to_dict() for c in self.find_conflicts(after)]
            items.append(item)
        return items

    def pending_count(self) -> int:
        return len(self.pending_with_conflicts())

    # ---------- 変更・取り消し ----------
    def change_candidates(self) -> list[Event]:
        """変更・取り消しの対象になりうる、今日以降の登録済みの予定（LLM にも渡す）。"""
        today = self.today()
        events = self.db.list_events(
            statuses=("confirmed", "pending"), date_from=today, date_to=today + timedelta(days=CANDIDATE_DAYS)
        )
        return events[:CANDIDATE_LIMIT]

    def resolve_target(self, raw: ExtractedEvent, candidates: list[Event]) -> tuple[Optional[Event], bool]:
        """変更・取り消しの対象を探す。戻り値は (対象の予定, はっきり特定できたか)。"""
        t_title = (raw.target_title or raw.title or "").strip()
        t_date = _parse_date(raw.target_date)
        if t_date is None and raw.action == "cancel":
            t_date = _parse_date(raw.date)  # 取り消しの date は対象の予定の日付

        by_id = {e.id: e for e in candidates}
        ev = by_id.get(raw.target_id) if raw.target_id else None
        if ev is not None:
            # 番号に加えてタイトル・日付も照らし合わせる（食い違いが多ければ確認に回す）
            title_ok = _similar(ev.title, t_title) if t_title else None
            date_ok = (ev.date == t_date) if t_date else None
            mismatch = (title_ok is False) + (date_ok is False)
            match = (title_ok is True) + (date_ok is True)
            return ev, mismatch == 0 or (mismatch == 1 and match == 1)

        if not t_title and not t_date:
            return None, False
        matches = [
            e
            for e in candidates
            if (t_date is None or e.date == t_date) and (not t_title or _similar(e.title, t_title))
        ]
        if not matches:
            return None, False
        # タイトルと日付の両方が一致する1件だけなら「はっきり」。候補が複数なら一番近いものを確認に回す
        return matches[0], len(matches) == 1 and bool(t_title) and t_date is not None

    def build_update(self, raw: ExtractedEvent, target: Event) -> Optional[Event]:
        """LLM の出力から変更後の予定を作る。空の項目は元の予定の値を引き継ぐ。"""
        d = _parse_date(raw.date) or target.date
        today = self.today()
        if d < today or d > today + timedelta(days=730):
            log.info("変更後の日付が範囲外のためスキップ: %s %s", target.title, d)
            return None
        start_raw = (raw.start_time or "").strip()
        start = (_parse_time(start_raw) if start_raw else None) or target.start_time
        end = _parse_time(raw.end_time) if (raw.end_time or "").strip() else None
        if end is None and start is not None:
            if start == target.start_time:
                end = target.end_time
            elif target.start_time and target.end_time:
                # 開始時刻だけ変わった場合は、元の予定の長さを保つ
                length = datetime.combine(target.date, target.end_time) - datetime.combine(target.date, target.start_time)
                shifted = datetime.combine(d, start) + length
                end = shifted.time() if shifted.date() == d else None
        if start is None or (end is not None and end <= start):
            end = None
        return replace(
            target,
            # タイトルは元のまま（LLM が「定例（延期）」のように書き換えても予定名は変えない）
            date=d,
            start_time=start,
            end_time=end,
            location=(raw.location or "").strip()[:200] or target.location,
            description=(raw.description or "").strip()[:500] or target.description,
        )

    async def handle_change(
        self, raw: ExtractedEvent, msg: IncomingMessage, candidates: list[Event]
    ) -> tuple[Optional[EventResult], str]:
        """LLM が見つけた変更・取り消しを処理する。戻り値は (結果, スキップした理由)。"""
        if raw.confidence < self.settings.min_confidence:
            log.info("確信度が低いためスキップ: %s %s (%.2f)", raw.action, raw.title, raw.confidence)
            return None, ""
        target, clear = self.resolve_target(raw, candidates)
        if target is None:
            if raw.action == "update":
                # 元の予定が登録されていなければ、変更後の内容を新しい予定として登録する
                event = self.validate(raw, msg)
                return (await self.add_event(event) if event else None), ""
            log.info("取り消し対象の予定が見つかりません: %s %s", raw.title, raw.date)
            return None, "取り消し対象の予定が見つかりませんでした"

        after: dict = {}
        if raw.action == "update":
            updated = self.build_update(raw, target)
            if updated is None:
                return None, "変更後の日付を解釈できませんでした"
            after = event_fields(updated)
            if after == event_fields(target):
                return EventResult(status="duplicate", event=target), ""  # 実質的に変わっていない
        change = EventChange(
            event_id=target.id,
            kind=raw.action,
            before=event_fields(target),
            after=after,
            source=msg.source,
            source_message_id=msg.message_id,
            source_author=msg.author,
            source_channel=msg.channel,
            source_url=msg.url,
            raw_text=msg.text[:2000],
            confidence=max(0.0, min(1.0, raw.confidence)),
        )
        return await self.submit_change(change, target, clear), ""

    def _confirm_reason(self, change: EventChange, clear: bool, conflicts: list[Event]) -> Optional[str]:
        """確認待ちにする理由。None なら自動で反映する。"""
        if conflicts:
            return "変更後の日時に別の予定があります"
        mode = self.settings.change_mode
        if mode == "confirm":
            return ""
        if mode == "auto":
            return None
        if not clear:
            return "どの予定のことか確定できませんでした"
        if change.confidence < self.settings.change_auto_confidence:
            return "変更内容に自信がないため確認しています"
        return None

    @staticmethod
    def _updated(target: Event, change: EventChange) -> Event:
        # 状態(status)は反映時点のものを保つ
        return with_fields(target, {k: v for k, v in change.after.items() if k != "status"})

    def _apply_locked(self, change: EventChange, target: Event) -> Event:
        """変更を予定に反映する（_save_lock 内で呼ぶ）。"""
        change.before = event_fields(target)  # 反映直前の状態を「元に戻す」用に保存
        updated = replace(target, status="cancelled") if change.kind == "cancel" else self._updated(target, change)
        self.db.update_event(updated)
        self.db.resolve_change(change, "applied")
        return updated

    async def submit_change(self, change: EventChange, target: Event, clear: bool = True) -> EventResult:
        async with self._save_lock:
            for p in self.db.list_changes(status="pending", event_id=target.id):
                if p.kind == change.kind and p.after == change.after:
                    return EventResult(status="change_pending", event=target, change=p)  # 同じ確認待ちがある
            conflicts = self.find_conflicts(self._updated(target, change)) if change.kind == "update" else []
            reason = self._confirm_reason(change, clear, conflicts)
            if reason is not None:
                change.reason = reason
            self.db.add_change(change)
            updated = self._apply_locked(change, target) if reason is None else None

        if updated is None:
            await self.notifier.change_pending(change, target, conflicts)
            return EventResult(status="change_pending", event=target, conflicts=conflicts, change=change)
        await self.notifier.changed(change, updated)
        return EventResult(status="updated" if change.kind == "update" else "cancelled", event=updated, change=change)

    async def apply_change(self, change_id: int) -> EventChange:
        """確認待ちの変更・取り消しを承認して反映する。"""
        async with self._save_lock:
            change = self.db.get_change(change_id)
            if change is None:
                raise KeyError(change_id)
            if change.status != "pending":
                return change
            target = self.db.get_event(change.event_id)
            if target is None or target.status in ("cancelled", "rejected"):
                self.db.resolve_change(change, "rejected")  # 対象がもうない
                return change
            updated = self._apply_locked(change, target)
        await self.notifier.changed(change, updated)
        return change

    async def reject_change(self, change_id: int) -> EventChange:
        change = self.db.get_change(change_id)
        if change is None:
            raise KeyError(change_id)
        if change.status != "pending":
            return change
        self.db.resolve_change(change, "rejected")
        await self.notifier.change_rejected(change)
        return change

    def is_undoable(self, change_id: int) -> bool:
        change = self.db.get_change(change_id)
        if change is None or change.status != "applied":
            return False
        latest = self.db.latest_applied_change(change.event_id)
        return latest is not None and latest.id == change.id and self.db.get_event(change.event_id) is not None

    async def undo_change(self, change_id: int) -> Event:
        """反映した変更・取り消しを元に戻す。"""
        async with self._save_lock:
            change = self.db.get_change(change_id)
            if change is None:
                raise KeyError(change_id)
            if change.status != "applied":
                raise ValueError("この変更は元に戻せません")
            latest = self.db.latest_applied_change(change.event_id)
            if latest is None or latest.id != change.id:
                raise ValueError("このあとにも変更があるため元に戻せません")
            target = self.db.get_event(change.event_id)
            if target is None:
                raise KeyError(change.event_id)
            restored = with_fields(target, change.before)
            self.db.update_event(restored)
            self.db.resolve_change(change, "undone")
        await self.notifier.undone(change, restored)
        return restored
