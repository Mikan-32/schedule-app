"""登録の基準（誰宛ての予定か・宛先ごとの扱い）のテスト。"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.audience import AudienceRules, clean_list, find_term, judge
from app.config import Settings
from app.db import Database
from app.llm.prompts import build_user_prompt
from app.main import create_app
from app.models import ContextMessage, IncomingMessage
from app.notifier import Notifier
from app.service import ScheduleService
from tests.conftest import FakeExtractor, RecordingSink
from tests.test_api import FakeLLM

TZ = Settings().tz
NAMES = ["遥陽", "はるひ", "みかん", "hal"]
GROUPS = ["students-2026"]
RULES = AudienceRules(names=NAMES, groups=GROUPS)


def msg(text, mid="m1", channel="Discord alpha-2026 #general", author="田中", source="winnotify", **kw):
    return IncomingMessage(source=source, message_id=mid, text=text, sent_at=datetime.now(TZ), author=author, channel=channel, **kw)


def ev(**kw):
    base = {"action": "add", "audience": "unknown", "title": "", "date": "", "start_time": "", "end_time": "", "confidence": 0.9}
    base.update(kw)
    return base


# ---------- 名前・グループの見つけ方 ----------
def test_find_term():
    assert find_term("⁨@遥陽⁩ 明日10時から", NAMES) == "遥陽"
    assert find_term("みかんさん、15日空いてる？", NAMES) == "みかん"
    assert find_term("HAL, tomorrow?", NAMES) == "hal"
    assert find_term("we shall meet", NAMES) is None  # 英数字の名前は単語の途中では一致しない
    assert find_term("＠ＳＴＵＤＥＮＴＳ－２０２６ 出欠確認", GROUPS) == "students-2026"  # 全角でも一致
    assert find_term("", NAMES) is None and find_term("遥陽", []) is None


def test_clean_list():
    assert clean_list([" @遥陽 ", "遥陽", "", "＠students-2026"]) == ["遥陽", "students-2026"]
    assert clean_list("遥陽, はるひ、みかん") == ["遥陽", "はるひ", "みかん"]


# ---------- 誰宛てか ----------
@pytest.mark.parametrize(
    "llm, kw, audience, reason",
    [
        ("others", {"text": "@遥陽 15日に面談です"}, "me", "名前「遥陽」が出てくる"),  # 名前があれば LLM の判断より優先
        ("unknown", {"text": "明日10時から", "author": "遥陽"}, "me", "自分の発言"),
        ("others", {"text": "田中さんは15日に面談です"}, "others", "他の人だけの予定"),
        ("others", {"text": "田中さんは15日に面談", "channel": "Discord DM"}, "others", "他の人だけの予定"),
        ("unknown", {"text": "明日遊ぼう", "channel": "Discord DM"}, "me", "DM"),
        ("unknown", {"text": "明日10時から", "source": "manual", "channel": "貼り付け"}, "me", "自分で貼り付けたメッセージ"),
        ("unknown", {"text": "了解、10時で", "reply_to": "遥陽: 明日何時？"}, "me", "自分のメッセージへの返信"),
        ("me", {"text": "⁨@students-2026⁩ 11月キャンプの出欠"}, "all", "所属グループ「students-2026」宛て"),
        ("unknown", {"text": "@everyone 今後の予定"}, "all", "@everyone 宛て"),
        ("unknown", {"text": "@here 本日22時からミーティング"}, "all", "@here 宛て"),
        ("all", {"text": "皆さん、15日は休講です"}, "all", "全体への連絡"),
        ("unknown", {"text": "@allison 明日10時"}, "unknown", "名前も所属グループも出てこない"),
        ("me", {"text": "明日10時からね"}, "unknown", "名前や DM などの根拠がない"),  # LLM の「自分宛て」だけでは足りない
        ("unknown", {"text": "@assistants 15日に打ち合わせ"}, "unknown", "名前も所属グループも出てこない"),
    ],
)
def test_judge(llm, kw, audience, reason):
    j = judge(llm, msg(**kw), RULES)
    assert (j.audience, j.reason) == (audience, reason)


def test_judge_uses_context_only_when_llm_says_me():
    context = [ContextMessage(author="田中", text="@遥陽 明日空いてる？", sent_at=datetime.now(TZ))]
    assert judge("me", msg("じゃあ14時で"), RULES, context).audience == "me"
    assert judge("unknown", msg("じゃあ14時で"), RULES, context).audience == "unknown"


def test_prompt_has_groups():
    sent = datetime(2026, 10, 16, 10, 0, tzinfo=TZ)
    p = build_user_prompt("x", sent, my_names=["遥陽"], my_groups=["students-2026"])
    assert "【所属グループ】students-2026（利用者が所属するグループ・ロール。ここへのメンションは all）" in p
    assert "【所属グループ】" not in build_user_prompt("x", sent, my_names=["遥陽"])


# ---------- 宛先ごとの扱い ----------
class HeldSink(RecordingSink):
    def __init__(self):
        super().__init__()
        self.held = []

    async def notify_held(self, event, message):
        self.held.append(message)


@pytest.fixture
def rules_env():
    settings = Settings(db_path=":memory:", discord_token="", conflict_mode="overlap", prefilter=True)
    db = Database(":memory:", tz=settings.tz)
    extractor = FakeExtractor()
    notifier = Notifier(db)
    sink = HeldSink()
    notifier.register(sink)
    service = ScheduleService(settings, db, extractor, notifier)
    service.save_audience_rules(
        {"names": NAMES, "groups": GROUPS, "actions": {"me": "add", "all": "confirm", "unknown": "confirm", "others": "skip"}}
    )
    return service, extractor, sink, db


async def test_actions_by_audience(rules_env, future):
    service, extractor, sink, db = rules_env

    # 自分宛て → そのまま登録
    extractor.events = [ev(title="ペンギンと遊ぶ", date=future(2).isoformat())]
    r = (await service.handle_message(msg("明日遊ぼう", "a1", channel="Discord DM", author="ペンギン"))).results[0]
    assert r.status == "added" and (r.event.audience, r.event.audience_reason) == ("me", "DM")

    # 全員・グループ宛て → 確認待ち（重なる予定がなくても）
    extractor.events = [ev(title="キャンプ出欠締切", date=future(3).isoformat(), audience="all")]
    r = (await service.handle_message(msg("@students-2026 出欠の締切は今週金曜です", "a2"))).results[0]
    assert r.status == "pending" and r.event.held and r.conflicts == []
    n = db.list_notifications()[0]
    assert n.kind == "held" and n.message.startswith("追加するか確認してください（全員・グループ宛て）: ")
    assert len(sink.held) == 1 and len(sink.added) == 1  # 確認待ちは「追加しました」を送らない
    item = service.pending_with_conflicts()[0]
    assert item["event"]["held"] and item["event"]["audience_label"] == "全員・グループ宛て"
    assert item["event"]["audience_reason"] == "所属グループ「students-2026」宛て" and item["conflicts"] == []
    await service.confirm(r.event.id)
    assert "キャンプ出欠締切" in [e.title for e in db.list_events()]

    # 誰宛てか分からない → 確認待ち。「追加しない」を選んだら、同じリマインドでもう一度は聞かない
    extractor.events = [ev(title="打ち上げ", date=future(5).isoformat(), start_time="19:00")]
    r = (await service.handle_message(msg("15日19時から打ち上げです", "a3"))).results[0]
    assert r.status == "pending" and r.event.audience == "unknown"
    await service.reject(r.event.id)
    r2 = (await service.handle_message(msg("リマインド: 15日19時から打ち上げ", "a4"))).results[0]
    assert r2.status == "duplicate" and service.pending_with_conflicts() == []

    # 他の人宛て → 見送り（通知に残り、あとから追加できる）
    extractor.events = [ev(title="面談", date=future(6).isoformat(), audience="others")]
    r = (await service.handle_message(msg("田中さんは面談です", "a5"))).results[0]
    assert r.status == "skipped" and db.list_notifications()[0].message.startswith("他の人の予定として見送りました: ")
    assert (await service.adopt(r.event.id)).status == "added"


async def test_held_event_merges_into_existing(rules_env, future):
    service, extractor, _, db = rules_env
    d = future(4)
    extractor.events = [ev(title="定例ミーティング", date=d.isoformat(), start_time="22:00")]
    assert (await service.handle_message(msg("@遥陽 22時から定例", "b1"))).results[0].status == "added"
    # 全員向けのリマインドでも、登録済みの同じ予定ならまとめる（確認待ちにしない）
    extractor.events = [ev(title="定例ミーティング", date=d.isoformat(), start_time="22:00", location="Zoom", audience="all")]
    r = (await service.handle_message(msg("@here 本日22時から定例です Zoom で", "b2"))).results[0]
    assert r.status == "merged" and service.pending_with_conflicts() == []


async def test_skip_label_and_me_confirm(rules_env, future):
    service, extractor, _, db = rules_env
    service.save_audience_rules({"actions": {"all": "skip", "me": "confirm"}})
    extractor.events = [ev(title="説明会", date=future(3).isoformat())]
    r = (await service.handle_message(msg("@everyone 来週水曜に説明会があります", "c1"))).results[0]
    assert r.status == "skipped"
    assert db.list_notifications()[0].message.startswith("全員・グループ宛ての予定として見送りました: ")
    extractor.events = [ev(title="ゼミ", date=future(4).isoformat())]
    assert (await service.handle_message(msg("遥陽、明日のゼミ来れる？", "c2"))).results[0].status == "pending"


def test_rules_are_saved_and_loaded(tmp_path):
    settings = Settings(db_path=tmp_path / "t.db", discord_token="")
    db = Database(tmp_path / "t.db", tz=settings.tz)
    service = ScheduleService(settings, db, FakeExtractor(), Notifier(db))
    saved = service.save_audience_rules({"names": ["@遥陽", "みかん", "みかん"], "groups": ["students-2026"], "actions": {"unknown": "skip"}})
    assert saved.names == ["遥陽", "みかん"] and saved.actions["unknown"] == "skip" and saved.actions["others"] == "skip"
    assert settings.my_names == ["遥陽", "みかん"] and settings.my_groups == ["students-2026"]  # LLM のプロンプトにも使われる

    fresh = Settings(db_path=tmp_path / "t.db", discord_token="")
    ScheduleService(fresh, db, FakeExtractor(), Notifier(db))  # 起動時に保存した基準を読み込む（.env より優先）
    assert fresh.my_names == ["遥陽", "みかん"] and fresh.audience_unknown == "skip" and fresh.my_groups == ["students-2026"]


def test_api_settings(tmp_path):
    settings = Settings(db_path=tmp_path / "t.db", discord_token="")
    with TestClient(create_app(settings, llm=FakeLLM(), start_connectors=False)) as c:
        r = c.get("/api/settings/audience").json()
        assert r["labels"]["unknown"] == "誰宛てか分からない" and set(r["actions"]) == {"me", "all", "unknown", "others"}
        body = {"names": ["遥陽", " はるひ "], "groups": ["@students-2026"], "actions": {"all": "confirm", "unknown": "confirm"}}
        r = c.put("/api/settings/audience", json=body).json()
        assert r["names"] == ["遥陽", "はるひ"] and r["groups"] == ["students-2026"] and r["actions"]["all"] == "confirm"
        assert c.get("/api/settings/audience").json()["groups"] == ["students-2026"]
        assert c.put("/api/settings/audience", json={"actions": {"all": "maybe"}}).status_code == 400
        assert c.put("/api/settings/audience", json={"actions": {"boss": "add"}}).status_code == 400
