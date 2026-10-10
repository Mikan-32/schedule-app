from datetime import datetime, time

from app.models import Event, ImageInput, IncomingMessage


def msg(text, images=None, mid="1"):
    from app.config import Settings

    return IncomingMessage(source="discord", message_id=mid, text=text, sent_at=datetime.now(Settings().tz), images=images or [])


async def test_added_notification(env, future):
    service, extractor, sink, db = env
    d = future(5)
    extractor.events = [{"title": "定例ミーティング", "date": d.isoformat(), "start_time": "14:00", "end_time": "15:00", "confidence": 0.9}]
    out = await service.handle_message(msg("来週の会議は14時からです"))
    assert [r.status for r in out.results] == ["added"]
    assert sink.added == [f"{d.month}月{d.day}日({'月火水木金土日'[d.weekday()]})14時から定例ミーティング"]
    assert db.list_notifications()[0].kind == "added"


async def test_conflict_goes_pending_then_confirm(env, future):
    service, extractor, sink, db = env
    d = future(3)
    await service.add_event(Event(title="定例", date=d, start_time=time(14, 0), end_time=time(15, 0)))
    extractor.events = [{"title": "歯医者", "date": d.isoformat(), "start_time": "14:30", "end_time": "", "confidence": 0.9}]
    out = await service.handle_message(msg("14時半から歯医者"))
    r = out.results[0]
    assert r.status == "pending" and [c.title for c in r.conflicts] == ["定例"]
    assert len(sink.conflicts) == 1 and "既に予定があります" in sink.conflicts[0]
    assert len(sink.added) == 1  # 「定例」の分のみ

    await service.confirm(r.event.id)
    assert db.get_event(r.event.id).status == "confirmed"
    assert len(sink.added) == 2 and "歯医者" in sink.added[-1]


async def test_reject(env, future):
    service, extractor, sink, db = env
    d = future(3)
    await service.add_event(Event(title="定例", date=d, start_time=time(14, 0)))
    res = await service.add_event(Event(title="面談", date=d, start_time=time(14, 30)))
    await service.reject(res.event.id)
    assert db.get_event(res.event.id).status == "rejected"
    assert [e.title for e in db.list_events()] == ["定例"]


async def test_same_day_but_not_overlapping_is_added(env, future):
    service, *_ = env
    d = future(2)
    await service.add_event(Event(title="会議", date=d, start_time=time(10, 0), end_time=time(11, 0)))
    res = await service.add_event(Event(title="飲み会", date=d, start_time=time(19, 0)))
    assert res.status == "added"


async def test_same_day_mode(env, future):
    service, *_ = env
    service.settings.conflict_mode = "same_day"
    d = future(2)
    await service.add_event(Event(title="会議", date=d, start_time=time(10, 0)))
    res = await service.add_event(Event(title="飲み会", date=d, start_time=time(19, 0)))
    assert res.status == "pending"


async def test_all_day_conflicts(env, future):
    service, *_ = env
    d = future(2)
    await service.add_event(Event(title="社員旅行", date=d))
    res = await service.add_event(Event(title="飲み会", date=d, start_time=time(19, 0)))
    assert res.status == "pending"


async def test_duplicate_is_skipped(env, future):
    service, _, sink, _ = env
    d = future(4)
    await service.add_event(Event(title="定例ミーティング", date=d, start_time=time(14, 0)))
    res = await service.add_event(Event(title="定例ミーティング（リマインド）", date=d, start_time=time(14, 0)))
    assert res.status == "duplicate"
    assert len(sink.added) == 1 and not sink.conflicts


async def test_prefilter_skips_chatter(env):
    service, extractor, *_ = env
    out = await service.handle_message(msg("おつかれさまです！ありがとうございます"))
    assert out.skipped and extractor.calls == []


async def test_prefilter_bypassed_with_image(env):
    service, extractor, *_ = env
    await service.handle_message(msg("これ", images=[ImageInput(data=b"x")]))
    assert len(extractor.calls) == 1


async def test_same_message_processed_once(env, future):
    service, extractor, *_ = env
    extractor.events = []
    await service.handle_message(msg("明日10時", mid="42"))
    out = await service.handle_message(msg("明日10時", mid="42"))
    assert out.skipped == "処理済みのメッセージです" and len(extractor.calls) == 1


async def test_validation_filters(env, future):
    service, extractor, *_ = env
    extractor.events = [
        {"title": "過去", "date": future(-3).isoformat(), "start_time": "10:00", "confidence": 0.9},
        {"title": "低確信", "date": future(3).isoformat(), "start_time": "10:00", "confidence": 0.2},
        {"title": "不正日付", "date": "来週", "start_time": "10:00", "confidence": 0.9},
        {"title": "全角時刻", "date": future(3).isoformat(), "start_time": "１５：００", "end_time": "14:00", "confidence": 0.9},
    ]
    out = await service.handle_message(msg("いろいろ 10時"))
    assert len(out.results) == 1
    ev = out.results[0].event
    assert ev.title == "全角時刻" and ev.start_time == time(15, 0) and ev.end_time is None


async def test_llm_failure_is_queued_and_retried(env, future):
    service, extractor, _, db = env
    original = extractor.extract

    async def boom(**kwargs):
        raise RuntimeError("down")

    extractor.extract = boom
    out = await service.handle_message(msg("明日10時から会議", mid="99"))
    assert out.queued and db.queue_counts()["waiting"] == 1
    # 同じメッセージがもう一度届いても二重には保存しない
    assert (await service.handle_message(msg("明日10時から会議", mid="99"))).skipped == "処理済みのメッセージです"

    extractor.extract = original
    extractor.events = [{"title": "会議", "date": future(1).isoformat(), "start_time": "10:00", "confidence": 0.9}]
    assert await service.process_queue() == 1
    assert db.queue_counts()["waiting"] == 0 and [e.title for e in db.list_events()] == ["会議"]
