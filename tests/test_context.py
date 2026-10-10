"""直前のやり取り（文脈）と、自分宛てかどうかの判定のテスト。"""
from datetime import datetime, time, timedelta, timezone

from fastapi.testclient import TestClient

from app.config import Settings
from app.llm.prompts import build_user_prompt
from app.main import create_app
from app.models import ContextMessage, Event, ExtractedEvent, IncomingMessage, thread_key
from app.service import is_followup
from tests.test_api import FakeLLM

TZ = Settings().tz


def msg(text, mid, channel="研究室 #連絡", author="田中", minutes_ago=0, **kw):
    sent = datetime.now(TZ) - timedelta(minutes=minutes_ago)
    return IncomingMessage(source="winnotify", message_id=mid, text=text, sent_at=sent, author=author, channel=channel, **kw)


def ev(**kw):
    base = {"action": "add", "audience": "unknown", "title": "", "date": "", "start_time": "", "end_time": "", "confidence": 0.9}
    base.update(kw)
    return base


# ---------- 文脈 ----------
async def test_context_is_passed_and_followup_is_analyzed(env, future):
    service, extractor, _, db = env
    # 予定の提案（まだ決まっていない → 何も抽出されない）
    await service.handle_message(msg("来週の定例、15日と16日どっちがいい？", "c1", minutes_ago=10))
    await service.handle_message(msg("16日は授業があります", "c2", author="佐藤", minutes_ago=5))
    # 日付の表現がない返事でも、予定の話の流れなら LLM に送る
    extractor.events = [ev(title="定例", date=future(5).isoformat(), start_time="14:00", audience="all")]
    out = await service.handle_message(msg("じゃあそれで決定で！", "c3"))
    assert [r.status for r in out.results] == ["added"]
    ctx = extractor.calls[-1]["context"]
    assert [(c.author, c.text) for c in ctx] == [("田中", "来週の定例、15日と16日どっちがいい？"), ("佐藤", "16日は授業があります")]


async def test_followup_without_schedule_context_is_skipped(env):
    service, extractor, *_ = env
    await service.handle_message(msg("資料ありがとうございます", "n1", minutes_ago=3))
    out = await service.handle_message(msg("了解です", "n2"))
    assert out.skipped and len(extractor.calls) == 0


async def test_context_is_separated_by_channel_and_dm_partner(env):
    service, extractor, _, db = env
    await service.handle_message(msg("明日10時に集合", "s1", channel="サークル #全体", minutes_ago=5))
    await service.handle_message(msg("明後日の15時どう？", "s2", channel="Discord DM", author="佐藤", minutes_ago=4))
    await service.handle_message(msg("明日の13時は？", "s3", channel="Discord DM", author="鈴木", minutes_ago=3))
    await service.handle_message(msg("OKです、明日で", "s4", channel="Discord DM", author="鈴木"))
    assert [c.text for c in extractor.calls[-1]["context"]] == ["明日の13時は？"]


async def test_old_messages_are_not_used(env):
    service, extractor, *_ = env
    service.settings.context_minutes = 60
    await service.handle_message(msg("明日10時に集合", "o1", minutes_ago=120))
    out = await service.handle_message(msg("了解です", "o2"))
    assert out.skipped and len(extractor.calls) == 1  # 1件目だけ LLM に送られた


async def test_reply_to_is_passed(env, future):
    service, extractor, *_ = env
    extractor.events = []
    await service.handle_message(msg("それでお願いします", "r1", reply_to="田中: 明日14時から打ち合わせでどうですか"))
    assert extractor.calls[-1]["reply_to"].startswith("田中: 明日14時")


async def test_queue_keeps_thread_and_reply(env):
    service, extractor, _, db = env

    async def down(**kwargs):
        raise RuntimeError("down")

    extractor.extract = down
    await service.handle_message(msg("明日10時で", "q1", thread="discord|123", reply_to="田中: 明日どう？"))
    [(qid, saved)] = db.list_queue()
    assert (saved.thread, saved.reply_to) == ("discord|123", "田中: 明日どう？")


def test_thread_key():
    assert thread_key(msg("x", "1", channel="研究室 #連絡")) == "winnotify|研究室 #連絡"
    assert thread_key(msg("x", "1", channel="Discord DM", author="佐藤")) == "winnotify|Discord DM|佐藤"
    assert thread_key(msg("x", "1", thread="discord|999")) == "discord|999"


def test_followup_words():
    assert is_followup("OKです") and is_followup("それでお願いします") and is_followup("参加します👍")
    assert not is_followup("お疲れさまです") and not is_followup("bookmark")


def test_prompt_includes_names_context_and_reply():
    sent = datetime(2026, 10, 10, 15, 0, tzinfo=TZ)
    ctx = [ContextMessage("田中", "来週の定例、15日と16日どっちがいい？", datetime(2026, 10, 10, 5, 50, tzinfo=timezone.utc))]
    p = build_user_prompt(
        "15日で決定", sent, author="佐藤", channel="Discord DM", context=ctx, reply_to="田中: どっち？", my_names=["遥陽", "みかん"]
    )
    assert "【利用者】遥陽 / みかん" in p
    assert "【チャンネル】Discord DM（利用者との1対1のメッセージ）" in p
    assert "【直前のやり取り】（同じ場所の直前のメッセージ。古い順。参考情報）\n10/10 14:50 田中: 来週の定例" in p
    assert "【返信先のメッセージ】\n田中: どっち？" in p
    assert "【利用者】（名前は未設定）" in build_user_prompt("x", sent)


# ---------- 自分宛てか ----------
def test_audience_is_lenient():
    assert ExtractedEvent(audience="Everyone").audience == "all"
    assert ExtractedEvent(audience="???").audience == "unknown"


async def test_others_event_is_skipped_and_can_be_adopted(env, future):
    service, extractor, sink, db = env
    d = future(5)
    extractor.events = [ev(title="面談", date=d.isoformat(), start_time="15:00", audience="others")]
    r = (await service.handle_message(msg("田中さんは15時から面談です", "a1"))).results[0]
    assert r.status == "skipped" and db.list_events() == [] and sink.added == []
    n = db.list_notifications()[0]
    assert n.kind == "skipped" and n.message.startswith("他の人の予定として見送りました: ") and n.message.endswith("（研究室 #連絡 ・ 田中）")

    # 同じ連絡がもう一度届いても、見送りは1件だけ
    r2 = (await service.handle_message(msg("田中さん、15時から面談ですよ", "a2"))).results[0]
    assert r2.status == "duplicate" and len(db.list_events(statuses=("skipped",))) == 1

    adopted = await service.adopt(r.event.id)
    assert adopted.status == "added" and [e.title for e in db.list_events()] == ["面談"]
    assert len(sink.added) == 1


async def test_adopting_into_conflict_and_into_same(env, future):
    service, extractor, _, db = env
    d = future(5)
    await service.add_event(Event(title="ゼミ", date=d, start_time=time(15, 0), end_time=time(16, 0)))
    extractor.events = [ev(title="面談", date=d.isoformat(), start_time="15:30", audience="others")]
    r = (await service.handle_message(msg("田中さんは15時半から面談", "b1"))).results[0]
    assert (await service.adopt(r.event.id)).status == "pending"

    extractor.events = [ev(title="ゼミ発表", date=d.isoformat(), start_time="15:00", location="301", audience="others")]
    r = (await service.handle_message(msg("佐藤さんはゼミ発表 15時 301教室", "b2"))).results[0]
    res = await service.adopt(r.event.id)
    assert res.status == "merged" and db.get_event(r.event.id).status == "merged"


async def test_me_all_unknown_and_add_mode_are_registered(env, future):
    service, extractor, _, db = env
    for i, aud in enumerate(("me", "all", "unknown")):
        extractor.events = [ev(title=f"予定{i}", date=future(3 + i).isoformat(), audience=aud)]
        assert (await service.handle_message(msg(f"明日の予定{i}", f"m{i}"))).results[0].status == "added"
    service.settings.others_mode = "add"
    extractor.events = [ev(title="面談", date=future(8).isoformat(), audience="others")]
    assert (await service.handle_message(msg("田中さんは面談", "m9"))).results[0].status == "added"


def test_api_adopt(tmp_path, future):
    settings = Settings(db_path=tmp_path / "t.db", discord_token="")
    llm = FakeLLM()
    with TestClient(create_app(settings, llm=llm, start_connectors=False)) as c:
        llm.events = [ev(title="面談", date=future(4).isoformat(), start_time="10:00", audience="others")]
        r = c.post("/api/analyze", data={"text": "田中さんは10時から面談です"}).json()
        assert r["results"][0]["status"] == "skipped"
        n = c.get("/api/notifications").json()[0]
        assert n["kind"] == "skipped" and n["adoptable"] is True
        assert c.post(f"/api/events/{n['event_id']}/adopt").json()["status"] == "added"
        assert c.get("/api/notifications").json()[1]["adoptable"] is False
        assert c.post(f"/api/events/{n['event_id']}/adopt").status_code == 409
        assert [e["title"] for e in c.get("/api/events").json()] == ["面談"]
