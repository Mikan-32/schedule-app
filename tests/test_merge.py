"""同じ予定をまとめる処理のテスト。"""
from datetime import datetime, time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import Event, IncomingMessage
from app.service import _similar
from tests.test_api import FakeLLM

WD = "月火水木金土日"


def msg(text, mid):
    return IncomingMessage(source="discord", message_id=mid, text=text, sent_at=datetime.now(Settings().tz))


def ev(action="add", **kw):
    base = {"action": action, "target_id": 0, "title": "", "date": "", "start_time": "", "end_time": "", "location": "", "confidence": 0.9}
    base.update(kw)
    return base


async def setup_event(service, d, title="定例ミーティング", start=time(14, 0), end=time(15, 0), location=""):
    return (await service.add_event(Event(title=title, date=d, start_time=start, end_time=end, location=location))).event


def test_title_similarity():
    assert _similar("定例ミーティング", "定例MTG")
    assert _similar("定例会議", "定例ミーティング")
    assert _similar("レポート提出締め切り", "レポート提出〆切")
    assert _similar("ＭＴＧ", "mtg")
    assert _similar("定例ミーティング", "【リマインド】定例ミーティング")
    assert not _similar("英語の授業", "数学の授業")
    assert not _similar("第3回ゼミ", "第4回ゼミ")
    assert not _similar("歯医者", "定例")


# ---------- 自動でまとめる ----------
async def test_reminder_with_different_wording_is_duplicate(env, future):
    service, extractor, sink, db = env
    d = future(3)
    await setup_event(service, d)
    extractor.events = [ev(title="定例MTG", date=d.isoformat(), start_time="14:00")]
    r = (await service.handle_message(msg("リマインド: 定例MTGは14時からです", "r1"))).results[0]
    assert r.status == "duplicate"
    assert service.pending_count() == 0 and sink.conflicts == [] and len(db.list_notifications()) == 1  # 追加時の1件だけ


async def test_merge_fills_missing_time_and_location(env, future):
    service, extractor, sink, db = env
    d = future(4)
    party = await setup_event(service, d, title="飲み会", start=None, end=None)
    extractor.events = [ev(title="飲み会", date=d.isoformat(), start_time="19:00", location="駅前の居酒屋")]
    r = (await service.handle_message(msg("飲み会は19時から駅前の居酒屋です", "r2"))).results[0]
    assert r.status == "merged" and r.event.id == party.id
    saved = db.get_event(party.id)
    assert (saved.start_time, saved.location) == (time(19, 0), "駅前の居酒屋")
    assert [e.id for e in db.list_events()] == [party.id]  # 予定は1件のまま
    n = db.list_notifications()[0]
    assert n.kind == "merged" and n.message == (
        f"同じ予定の連絡をまとめました: {d.month}月{d.day}日({WD[d.weekday()]})19時から飲み会に時刻「19:00」・場所「駅前の居酒屋」を追加"
    )
    assert sink.changed == [n.message] and service.is_undoable(r.change.id)

    await service.undo_change(r.change.id)
    saved = db.get_event(party.id)
    assert saved.start_time is None and saved.location == ""


async def test_merge_adds_end_time(env, future):
    service, extractor, _, db = env
    d = future(4)
    e = await setup_event(service, d, end=None)
    extractor.events = [ev(title="定例ミーティング", date=d.isoformat(), start_time="14:00", end_time="15:30")]
    r = (await service.handle_message(msg("定例は14時〜15時半です", "r3"))).results[0]
    assert r.status == "merged" and db.get_event(e.id).end_time == time(15, 30)


async def test_merge_does_not_overwrite_existing_info(env, future):
    service, extractor, _, db = env
    d = future(4)
    e = await setup_event(service, d, location="会議室A")
    extractor.events = [ev(title="定例ミーティング", date=d.isoformat(), start_time="14:00", location="会議室B")]
    r = (await service.handle_message(msg("定例 14時 会議室B", "r4"))).results[0]
    assert r.status == "duplicate" and db.get_event(e.id).location == "会議室A"


# ---------- LLM の same 判定 ----------
async def test_llm_same_merges_differently_named_event(env, future):
    service, extractor, _, db = env
    d = future(5)
    e = await setup_event(service, d)
    extractor.events = [ev("same", target_id=e.id, title="週次の集まり", date=d.isoformat(), start_time="14:00", location="会議室B")]
    r = (await service.handle_message(msg("週次の集まり、会議室Bでやります", "s1"))).results[0]
    assert r.status == "merged" and r.event.id == e.id
    assert db.get_event(e.id).title == "定例ミーティング" and db.get_event(e.id).location == "会議室B"
    assert service.pending_count() == 0


async def test_llm_same_without_date_uses_target_date(env, future):
    service, extractor, *_ = env
    e = await setup_event(service, future(5))
    extractor.events = [ev("same", target_id=e.id, title="定例")]
    r = (await service.handle_message(msg("例の定例、予定どおりです", "s2"))).results[0]
    assert r.status == "duplicate" and r.event.id == e.id


async def test_llm_same_with_different_date_is_treated_as_new(env, future):
    service, extractor, _, db = env
    e = await setup_event(service, future(5))
    extractor.events = [ev("same", target_id=e.id, title="定例ミーティング", date=future(6).isoformat(), start_time="14:00")]
    r = (await service.handle_message(msg("定例は6日後の14時", "s3"))).results[0]
    assert r.status == "added" and len(db.list_events()) == 2


async def test_llm_same_with_conflicting_time_asks_then_merge(env, future):
    service, extractor, sink, db = env
    d = future(5)
    e = await setup_event(service, d)
    extractor.events = [ev("same", target_id=e.id, title="週次定例", date=d.isoformat(), start_time="14:30")]
    r = (await service.handle_message(msg("週次定例は14時半から", "s4"))).results[0]
    assert r.status == "pending" and [c.id for c in r.conflicts] == [e.id]
    item = service.pending_with_conflicts()[0]
    assert item["merge_into"] == e.id and item["likely_same"] is False

    merged = await service.merge_pending(r.event.id)
    assert merged.id == e.id and db.get_event(r.event.id).status == "merged"
    assert db.get_event(e.id).start_time == time(14, 0)  # 既存の時刻は変えない
    assert service.pending_count() == 0
    assert db.list_notifications()[0].message.startswith("同じ予定としてまとめました: ")
    with pytest.raises(ValueError):
        await service.merge_pending(r.event.id)


# ---------- 確認待ちから手動でまとめる ----------
async def test_same_start_time_is_flagged_as_likely_same(env, future):
    service, extractor, sink, db = env
    d = future(5)
    other = await setup_event(service, d, title="歯医者", start=time(13, 0), end=time(15, 0))
    e = await setup_event(service, d, title="定例", end=None)
    assert e.status == "pending"  # 歯医者と重なる
    extractor.events = [ev(title="週次の集まり", date=d.isoformat(), start_time="14:00", end_time="15:00", location="会議室B")]
    r = (await service.handle_message(msg("週次の集まり 14-15時 会議室B", "m1"))).results[0]
    assert r.status == "pending" and [c.id for c in r.conflicts] == [e.id, other.id]  # 開始が同じ「定例」が先頭
    assert sink.conflicts[-1].startswith(f"{d.month}月{d.day}日({WD[d.weekday()]})に同じ予定があるかもしれません")

    merged = await service.merge_pending(r.event.id)
    assert merged.id == e.id and (merged.end_time, merged.location) == (time(15, 0), "会議室B")
    assert db.list_notifications()[0].kind == "merged"


async def test_merge_pending_errors(env, future):
    service, *_ = env
    e = await setup_event(service, future(5))
    with pytest.raises(ValueError):
        await service.merge_pending(e.id)  # 確認待ちではない
    with pytest.raises(KeyError):
        await service.merge_pending(9999)


def test_api_merge(tmp_path, future):
    settings = Settings(db_path=tmp_path / "t.db", discord_token="")
    llm = FakeLLM()
    d = future(6).isoformat()
    with TestClient(create_app(settings, llm=llm, start_connectors=False)) as c:
        c.post("/api/events", json={"title": "定例", "date": d, "start_time": "14:00"})
        r = c.post("/api/events", json={"title": "週次の集まり", "date": d, "start_time": "14:00", "location": "会議室B"}).json()
        assert r["status"] == "pending"
        pending = c.get("/api/pending").json()[0]
        assert pending["likely_same"] is True

        merged = c.post(f"/api/events/{r['event']['id']}/merge?into={pending['merge_into']}").json()
        assert merged["title"] == "定例" and merged["location"] == "会議室B"
        assert c.post(f"/api/events/{r['event']['id']}/merge").status_code == 409
        assert [e["title"] for e in c.get("/api/events").json()] == ["定例"]
        notes = c.get("/api/notifications").json()
        assert notes[0]["kind"] == "merged" and notes[0]["undoable"] is True

        # 手動入力でも同じ予定ならまとめる
        r = c.post("/api/events", json={"title": "定例会議", "date": d, "start_time": "14:00", "end_time": "15:00"}).json()
        assert r["status"] == "merged" and r["event"]["end_time"] == "15:00"
