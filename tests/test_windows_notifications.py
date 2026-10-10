"""Windows 通知コネクタのテスト（Windows 以外でも動くよう、通知APIとDBは偽物を使う）。"""
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.config import Settings
from app.connectors import build_connectors
from app.connectors.windows_notifications import (
    DatabaseSource,
    NotificationSource,
    ToastNotification,
    WindowsNotificationConnector,
    WinRTSource,
    filetime_to_datetime,
    parse_discord_title,
    parse_toast_xml,
)

DISCORD_XML = """<toast launch="discord://"><visual><binding template="ToastGeneric">
<text>山田 (#連絡, 研究室)</text><text>来週水曜15時からゼミです</text>
<image placement="appLogoOverride" src="C:\\avatar.png"/></binding></visual></toast>"""


def to_filetime(dt: datetime) -> int:
    return int((dt - datetime(1601, 1, 1, tzinfo=timezone.utc)).total_seconds() * 10_000_000)


def test_parse_toast_xml():
    assert parse_toast_xml(DISCORD_XML.encode()) == ["山田 (#連絡, 研究室)", "来週水曜15時からゼミです"]
    assert parse_toast_xml(b"not xml") == []


def test_parse_discord_title():
    assert parse_discord_title("山田 (#連絡, 研究室)") == ("山田", "研究室 #連絡")
    assert parse_discord_title("佐藤") == ("佐藤", "DM")


def test_filetime():
    dt = datetime(2026, 10, 10, 1, 2, 3, tzinfo=timezone.utc)
    assert filetime_to_datetime(to_filetime(dt)) == dt


# ---------- 通知データベース ----------
def make_db(path: Path, rows):
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE NotificationHandler (RecordId INTEGER PRIMARY KEY, PrimaryId TEXT);
           CREATE TABLE Notification ("Order" INTEGER PRIMARY KEY, Id INTEGER, HandlerId INTEGER, Type TEXT,
               Payload BLOB, PayloadType TEXT, ArrivalTime INTEGER);
           INSERT INTO NotificationHandler VALUES (1, 'com.squirrel.Discord.Discord'), (2, 'com.squirrel.slack.slack');"""
    )
    for r in rows:
        conn.execute("INSERT INTO Notification (Id, HandlerId, Type, Payload, PayloadType, ArrivalTime) VALUES (?,?,?,?,?,?)", r)
    conn.commit()
    conn.close()


async def test_database_source(tmp_path):
    now = datetime.now(timezone.utc)
    db = tmp_path / "wpndatabase.db"
    make_db(db, [(1, 1, "toast", DISCORD_XML.encode(), "Xml", to_filetime(now)), (2, 1, "tile", b"<tile/>", "Xml", 0)])
    src = DatabaseSource(db)
    await src.open()
    first = await src.fetch()
    assert len(first) == 1
    t = first[0]
    assert t.app_id == "com.squirrel.Discord.Discord" and t.title == "山田 (#連絡, 研究室)"
    assert abs((t.created_at - now).total_seconds()) < 1
    assert await src.fetch() == []  # 前回以降の新着のみ

    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO Notification (Id, HandlerId, Type, Payload, PayloadType, ArrivalTime) VALUES (3, 2, 'toast', ?, 'Xml', ?)",
                 (b"<toast><visual><binding template='ToastGeneric'><text>#general</text><text>a</text></binding></visual></toast>", to_filetime(now)))
    conn.commit()
    conn.close()
    assert [t.app_id for t in await src.fetch()] == ["com.squirrel.slack.slack"]


async def test_database_source_missing(tmp_path):
    with pytest.raises(FileNotFoundError):
        await DatabaseSource(tmp_path / "none.db").open()


# ---------- コネクタ ----------
class FakeSource(NotificationSource):
    name = "winrt"

    def __init__(self, toasts=None, fail=False):
        self.toasts = toasts or []
        self.fail = fail

    async def open(self):
        if self.fail:
            raise PermissionError("denied")

    async def fetch(self):
        return list(self.toasts)


def toast(key, texts, app="Discord", minutes_ago=0):
    return ToastNotification(key=key, app_name=app, app_id=app.lower(), texts=texts,
                             created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))


async def test_connector_flow(env, future):
    service, extractor, sink, db = env
    service.settings.windows_notify_apps = ["Discord"]
    d = future(4)
    extractor.events = [{"title": "ゼミ", "date": d.isoformat(), "start_time": "15:00", "confidence": 0.9}]
    src = FakeSource([
        toast("old", ["山田 (#連絡, 研究室)", "昨日の件、明日10時からで"], minutes_ago=120),  # 起動前の古い通知
        toast("a", ["山田 (#連絡, 研究室)", "来週水曜15時からゼミです"]),
        toast("s", ["#general", "明日10時から"], app="Slack"),  # 対象外アプリ
    ])
    conn = WindowsNotificationConnector(service.settings, service, sources=[src])
    await conn.start()
    conn._task.cancel()  # ループは使わず poll_once を直接呼ぶ
    assert conn.status()["state"] == "listening" and conn.status()["backend"] == "winrt"

    queued = await conn.poll_once()
    assert [m.message_id for m in queued] == ["a"]
    m = queued[0]
    assert (m.source, m.author, m.channel, m.text) == ("winnotify", "山田", "Discord 研究室 #連絡", "来週水曜15時からゼミです")
    for job in list(conn._jobs):
        await job
    assert [e.title for e in db.list_events()] == ["ゼミ"]
    assert len(sink.added) == 1

    # 同じ通知は二度処理しない。新着は処理する
    assert await conn.poll_once() == []
    src.toasts.append(toast("b", ["佐藤", "明日の打ち合わせは14時から"]))
    assert [m.message_id for m in await conn.poll_once()] == ["b"]
    await conn.stop()


async def test_connector_falls_back(env, tmp_path):
    service, *_ = env
    db = tmp_path / "wpndatabase.db"
    make_db(db, [])
    conn = WindowsNotificationConnector(service.settings, service, sources=[FakeSource(fail=True), DatabaseSource(db)])
    await conn.start()
    assert conn.status()["backend"] == "database"
    await conn.stop()


async def test_connector_all_fail(env):
    service, *_ = env
    conn = WindowsNotificationConnector(service.settings, service, sources=[FakeSource(fail=True)])
    await conn.start()
    assert conn.status()["state"] == "error" and "denied" in conn.status()["error"]


def test_build_connectors(env):
    service, _, _, _ = env
    s = Settings(discord_token="", windows_notifications="true")
    assert [c.name for c in build_connectors(s, service, service.notifier)] == ["windows_notifications"]
    s = Settings(discord_token="", windows_notifications="auto")
    expected = ["windows_notifications"] if sys.platform == "win32" else []
    assert [c.name for c in build_connectors(s, service, service.notifier)] == expected


# ---------- WinRT（PyWinRT を偽装して API 呼び出しを確認） ----------
@pytest.fixture
def fake_winrt(monkeypatch):
    async def done(value):
        return value

    class Text:
        def __init__(self, text):
            self.text = text

    class Binding:
        def __init__(self, texts):
            self._texts = texts

        def get_text_elements(self):
            return [Text(t) for t in self._texts]

    def make_notification(nid, app, texts):
        visual = types.SimpleNamespace(get_binding=lambda name: Binding(texts) if name == "ToastGeneric" else None)
        return types.SimpleNamespace(
            id=nid,
            creation_time=datetime(2026, 10, 10, 1, 0, tzinfo=timezone.utc),
            app_info=types.SimpleNamespace(display_info=types.SimpleNamespace(display_name=app), app_user_model_id=f"{app}.id"),
            notification=types.SimpleNamespace(visual=visual),
        )

    state = {"status": 1, "items": [make_notification(7, "Discord", ["山田", "明日10時"])]}

    class Listener:
        def request_access_async(self):
            return done(state["status"])

        def get_notifications_async(self, kinds):
            assert kinds == 1
            return done(state["items"])

    mgmt = types.ModuleType("winrt.windows.ui.notifications.management")
    mgmt.UserNotificationListener = types.SimpleNamespace(current=Listener())
    mgmt.UserNotificationListenerAccessStatus = types.SimpleNamespace(ALLOWED=1, DENIED=2)
    notif = types.ModuleType("winrt.windows.ui.notifications")
    notif.NotificationKinds = types.SimpleNamespace(TOAST=1)
    notif.management = mgmt
    ui = types.ModuleType("winrt.windows.ui")
    ui.notifications = notif
    windows = types.ModuleType("winrt.windows")
    windows.ui = ui
    winrt = types.ModuleType("winrt")
    winrt.windows = windows
    for name, mod in {"winrt": winrt, "winrt.windows": windows, "winrt.windows.ui": ui,
                      "winrt.windows.ui.notifications": notif, "winrt.windows.ui.notifications.management": mgmt}.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return state


async def test_winrt_source(fake_winrt):
    src = WinRTSource()
    await src.open()
    [t] = await src.fetch()
    assert (t.app_name, t.app_id, t.texts) == ("Discord", "Discord.id", ["山田", "明日10時"])
    assert t.key.startswith("winrt-7-")


async def test_winrt_access_denied(fake_winrt):
    fake_winrt["status"] = 2
    with pytest.raises(PermissionError):
        await WinRTSource().open()
