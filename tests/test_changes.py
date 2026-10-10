"""予定の変更・取り消しのテスト。"""
import sqlite3
from datetime import date, datetime, time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.llm.prompts import build_user_prompt
from app.main import create_app
from app.models import Event, ExtractedEvent, IncomingMessage
from app.service import looks_like_schedule
from tests.test_api import FakeLLM

WD = "月火水木金土日"


def msg(text, mid):
    return IncomingMessage(source="discord", message_id=mid, text=text, sent_at=datetime.now(Settings().tz))


def change(action, target_id=0, **kw):
    base = {"action": action, "target_id": target_id, "title": "", "date": "", "start_time": "", "end_time": "", "confidence": 0.9}
    base.update(kw)
    return base


async def setup_event(service, d, title="定例ミーティング", start=time(14, 0), end=time(15, 0)):
    return (await service.add_event(Event(title=title, date=d, start_time=start, end_time=end))).event


# ---------- 自動で反映 ----------
async def test_update_applied_automatically(env, future):
    service, extractor, sink, db = env
    d, d2 = future(3), future(4)
    ev = await setup_event(service, d)
    extractor.events = [
        change("update", ev.id, target_title="定例ミーティング", target_date=d.isoformat(),
               title="定例ミーティング（延期）", date=d2.isoformat(), start_time="16:00")
    ]
    out = await service.handle_message(msg("定例は翌日の16時に延期します", "m1"))
    r = out.results[0]
    assert r.status == "updated"
    saved = db.get_event(ev.id)
    # タイトルは元のまま、終了時刻は元の長さ(1時間)を保つ
    assert (saved.title, saved.date, saved.start_time, saved.end_time) == ("定例ミーティング", d2, time(16, 0), time(17, 0))
    assert saved.updated_at is not None
    n = db.list_notifications()[0]
    assert n.kind == "updated" and n.change_id == r.change.id
    assert n.message == (
        f"{d2.month}月{d2.day}日({WD[d2.weekday()]})16時から定例ミーティングに変更しました"
        f"（変更前: {d.month}月{d.day}日({WD[d.weekday()]})14時）"
    )
    assert sink.changed == [n.message]
    assert [e.id for e in extractor.calls[0]["existing"]] == [ev.id]  # LLM に登録済みの予定を渡している


async def test_cancel_applied_and_undo(env, future):
    service, extractor, sink, db = env
    d = future(2)
    ev = await setup_event(service, d)
    extractor.events = [change("cancel", ev.id, title="定例ミーティング", date=d.isoformat(), start_time="14:00")]
    r = (await service.handle_message(msg("明後日の定例は中止です", "m2"))).results[0]
    assert r.status == "cancelled"
    assert db.get_event(ev.id).status == "cancelled" and db.list_events() == []
    assert db.list_notifications()[0].message.endswith("14時から定例ミーティングを取り消しました")
    assert service.is_undoable(r.change.id)

    restored = await service.undo_change(r.change.id)
    assert restored.status == "confirmed" and [e.id for e in db.list_events()] == [ev.id]
    assert db.get_change(r.change.id).status == "undone"
    assert db.list_notifications()[0].kind == "undone"
    with pytest.raises(ValueError):
        await service.undo_change(r.change.id)


async def test_only_latest_change_can_be_undone(env, future):
    service, extractor, _, db = env
    ev = await setup_event(service, future(3))
    extractor.events = [change("update", ev.id, target_title="定例ミーティング", date=future(4).isoformat())]
    first = (await service.handle_message(msg("4日後に変更", "a"))).results[0]
    extractor.events = [change("update", ev.id, target_title="定例ミーティング", date=future(5).isoformat())]
    second = (await service.handle_message(msg("やっぱり5日後に変更", "b"))).results[0]
    assert first.status == second.status == "updated"
    assert not service.is_undoable(first.change.id)
    with pytest.raises(ValueError):
        await service.undo_change(first.change.id)
    await service.undo_change(second.change.id)
    assert db.get_event(ev.id).date == future(4)
    assert service.is_undoable(first.change.id)  # 1つ前の変更は元に戻せるようになる


async def test_wrong_target_id_falls_back_to_title_and_date(env, future):
    service, extractor, _, db = env
    d = future(3)
    ev = await setup_event(service, d)
    extractor.events = [change("cancel", 999, target_title="定例ミーティング", target_date=d.isoformat())]
    r = (await service.handle_message(msg("定例中止", "n"))).results[0]
    assert r.status == "cancelled" and r.event.id == ev.id


async def test_update_without_registered_target_is_added(env, future):
    service, extractor, _, db = env
    d = future(5)
    extractor.events = [change("update", 0, target_title="打ち上げ", title="打ち上げ", date=d.isoformat(), start_time="19:00")]
    r = (await service.handle_message(msg("打ち上げは19時からに変更です", "f"))).results[0]
    assert r.status == "added" and [e.title for e in db.list_events()] == ["打ち上げ"]


async def test_cancel_without_target_is_skipped(env, future):
    service, extractor, *_ = env
    extractor.events = [change("cancel", 0, target_title="飲み会", title="飲み会", date=future(2).isoformat())]
    out = await service.handle_message(msg("飲み会中止で", "g"))
    assert out.results == [] and out.skipped == "取り消し対象の予定が見つかりませんでした"


async def test_update_with_same_values_is_duplicate(env, future):
    service, extractor, sink, db = env
    d = future(3)
    ev = await setup_event(service, d)
    extractor.events = [change("update", ev.id, title="定例ミーティング", date=d.isoformat(), start_time="14:00", end_time="15:00")]
    r = (await service.handle_message(msg("定例は予定どおり14時からです", "h"))).results[0]
    assert r.status == "duplicate" and db.list_changes() == [] and sink.changed == []


# ---------- 確認待ちに回すケース ----------
async def test_ambiguous_target_goes_pending(env, future):
    service, extractor, sink, db = env
    a = await setup_event(service, future(2), title="ゼミ", start=time(10, 0), end=time(11, 0))
    b = await setup_event(service, future(9), title="ゼミ", start=time(10, 0), end=time(11, 0))
    extractor.events = [change("cancel", 0, target_title="ゼミ")]
    r = (await service.handle_message(msg("ゼミは中止になりました", "c"))).results[0]
    assert r.status == "change_pending" and r.event.id == a.id  # 一番近い予定を候補にする
    assert r.change.reason == "どの予定のことか確定できませんでした"
    assert db.get_event(a.id).status == "confirmed"  # まだ反映しない
    assert len(sink.change_pending) == 1 and sink.change_pending[0].startswith("ゼミの予定を取り消しますか？")
    assert [p["kind"] for p in service.pending_with_conflicts()] == ["cancel"] and service.pending_count() == 1

    applied = await service.apply_change(r.change.id)
    assert applied.status == "applied"
    assert db.get_event(a.id).status == "cancelled" and db.get_event(b.id).status == "confirmed"
    assert service.pending_count() == 0 and sink.changed  # 反映したら通知


async def test_target_id_contradicting_title_and_date_asks(env, future):
    service, extractor, _, db = env
    ev = await setup_event(service, future(3))
    extractor.events = [change("cancel", ev.id, target_title="歯医者", target_date=future(6).isoformat())]
    r = (await service.handle_message(msg("歯医者キャンセル", "o"))).results[0]
    assert r.status == "change_pending" and db.get_event(ev.id).status == "confirmed"


async def test_low_confidence_change_goes_pending(env, future):
    service, extractor, _, db = env
    ev = await setup_event(service, future(3))
    extractor.events = [change("update", ev.id, target_title="定例ミーティング", start_time="17:00", confidence=0.6)]
    r = (await service.handle_message(msg("定例17時からになりそうです", "d"))).results[0]
    assert r.status == "change_pending" and r.change.reason == "変更内容に自信がないため確認しています"
    assert db.get_event(ev.id).start_time == time(14, 0)


async def test_update_into_conflict_asks_and_reject_keeps_event(env, future):
    service, extractor, sink, db = env
    d, d2 = future(3), future(4)
    ev = await setup_event(service, d)
    other = await setup_event(service, d2, title="歯医者", start=time(16, 0), end=time(17, 0))
    extractor.events = [
        change("update", ev.id, target_title="定例ミーティング", target_date=d.isoformat(), date=d2.isoformat(), start_time="16:00")
    ]
    r = (await service.handle_message(msg("定例は翌日16時に変更", "e"))).results[0]
    assert r.status == "change_pending" and [c.id for c in r.conflicts] == [other.id]
    assert "重なる予定: " in sink.change_pending[0]

    rejected = await service.reject_change(r.change.id)
    assert rejected.status == "rejected"
    saved = db.get_event(ev.id)
    assert (saved.date, saved.start_time) == (d, time(14, 0))
    assert db.list_notifications()[0].message.startswith("変更しませんでした: ")


async def test_confirm_mode_always_asks(env, future):
    service, extractor, *_ = env
    service.settings.change_mode = "confirm"
    d = future(3)
    ev = await setup_event(service, d)
    extractor.events = [change("cancel", ev.id, target_title="定例ミーティング", target_date=d.isoformat())]
    r = (await service.handle_message(msg("定例は中止", "i"))).results[0]
    assert r.status == "change_pending" and r.change.reason == ""


async def test_auto_mode_applies_even_if_ambiguous(env, future):
    service, extractor, _, db = env
    service.settings.change_mode = "auto"
    a = await setup_event(service, future(2), title="ゼミ")
    await setup_event(service, future(9), title="ゼミ")
    extractor.events = [change("cancel", 0, target_title="ゼミ", confidence=0.6)]
    r = (await service.handle_message(msg("ゼミ休講", "p"))).results[0]
    assert r.status == "cancelled" and db.get_event(a.id).status == "cancelled"


async def test_same_pending_change_is_not_duplicated(env, future):
    service, extractor, sink, _ = env
    service.settings.change_mode = "confirm"
    ev = await setup_event(service, future(3))
    extractor.events = [change("cancel", ev.id, target_title="定例ミーティング")]
    r1 = (await service.handle_message(msg("定例中止", "j"))).results[0]
    r2 = (await service.handle_message(msg("定例は中止です（再掲）", "k"))).results[0]
    assert r1.change.id == r2.change.id and len(sink.change_pending) == 1


async def test_deleting_event_drops_pending_change(env, future):
    service, extractor, _, db = env
    service.settings.change_mode = "confirm"
    ev = await setup_event(service, future(3))
    extractor.events = [change("cancel", ev.id, target_title="定例ミーティング")]
    r = (await service.handle_message(msg("定例中止", "q"))).results[0]
    db.delete_event(ev.id)
    assert service.pending_count() == 0 and db.get_change(r.change.id).status == "rejected"


# ---------- プロンプト・前処理・DB ----------
def test_prefilter_catches_change_words():
    assert looks_like_schedule("例の件、延期になりました")
    assert looks_like_schedule("キャンセルでお願いします")


def test_prompt_lists_registered_events():
    ev = Event(id=7, title="定例ミーティング", date=date(2026, 10, 15), start_time=time(14, 0), end_time=time(15, 0), location="会議室A")
    prompt = build_user_prompt("中止です", datetime(2026, 10, 10, 9, 0), existing=[ev])
    assert "【登録済みの予定】\n#7 2026-10-15(木) 14:00-15:00 定例ミーティング @会議室A" in prompt
    assert "【登録済みの予定】\n（なし）" in build_user_prompt("x", datetime(2026, 10, 10))


def test_extracted_event_is_lenient():
    e = ExtractedEvent(action="Delete", target_id="#12")
    assert (e.action, e.target_id) == ("cancel", 12)
    assert ExtractedEvent(action="???", target_id=None).model_dump()["action"] == "add"


def test_old_database_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, date TEXT NOT NULL,
          start_time TEXT, end_time TEXT, location TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'confirmed', source TEXT NOT NULL DEFAULT 'manual',
          source_message_id TEXT NOT NULL DEFAULT '', source_author TEXT NOT NULL DEFAULT '',
          source_channel TEXT NOT NULL DEFAULT '', source_url TEXT NOT NULL DEFAULT '', raw_text TEXT NOT NULL DEFAULT '',
          confidence REAL NOT NULL DEFAULT 1.0, created_at TEXT NOT NULL);
        CREATE TABLE notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, message TEXT NOT NULL,
          event_id INTEGER, read INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
        INSERT INTO events (title, date, created_at) VALUES ('旧データ', '2030-01-01', '2026-10-01T00:00:00');
        INSERT INTO notifications (kind, message, created_at) VALUES ('added', '旧通知', '2026-10-01T00:00:00');
        """
    )
    con.commit()
    con.close()

    db = Database(path)
    ev = db.get_event(1)
    assert ev.title == "旧データ" and ev.updated_at is None
    assert db.list_notifications()[0].change_id is None
    db.update_event(ev)
    assert db.get_event(1).updated_at is not None and db.list_changes() == []
    db.close()


# ---------- API ----------
def test_api_change_flow(tmp_path, future):
    settings = Settings(db_path=tmp_path / "t.db", discord_token="")
    llm = FakeLLM()
    d = future(6).isoformat()
    with TestClient(create_app(settings, llm=llm, start_connectors=False)) as c:
        c.post("/api/events", json={"title": "定例", "date": d, "start_time": "14:00"})
        # 番号も日付も分からない → 確認待ち
        llm.events = [change("update", 0, target_title="定例", start_time="15:00")]
        r = c.post("/api/analyze", data={"text": "定例は15時からに変更"}).json()
        assert r["results"][0]["status"] == "change_pending"

        pending = c.get("/api/pending").json()
        assert pending[0]["kind"] == "update" and pending[0]["after"]["start_time"] == "15:00"
        assert c.get("/api/status").json()["pending_count"] == 1
        cid = pending[0]["change"]["id"]

        assert c.post(f"/api/changes/{cid}/apply").json()["status"] == "applied"
        assert c.get("/api/events").json()[0]["start_time"] == "15:00"
        notes = c.get("/api/notifications").json()
        assert notes[0]["kind"] == "updated" and notes[0]["undoable"] is True

        assert c.post(f"/api/changes/{cid}/undo").json()["start_time"] == "14:00"
        assert c.post(f"/api/changes/{cid}/undo").status_code == 409
        assert [n["undoable"] for n in c.get("/api/notifications").json()][:2] == [False, False]
        assert c.post("/api/changes/9999/apply").status_code == 404
