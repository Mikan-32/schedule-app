"""Linux のデスクトップ通知（D-Bus）の読み取りのテスト。dbus-daemon があれば本物のセッションバスでも確かめる。"""
import asyncio
import shutil
import subprocess

import pytest

from app.connectors import linux_notifications as linux
from app.connectors.linux_notifications import DBusSource, clean_markup, parse_call
from app.connectors.notifications import NotificationConnector


class V:
    """dbus-fast の Variant のかわり。"""

    def __init__(self, value):
        self.value = value


def test_parse_freedesktop_notify():
    body = ["discord", 0, "", "山田 (#連絡, 研究室)", "来週 &lt;水曜&gt; 15時から &amp; <b>ゼミ</b>", [], {"desktop-entry": V("discord")}, -1]
    assert parse_call("org.freedesktop.Notifications", "Notify", body) == (
        "discord", "discord", "山田 (#連絡, 研究室)", "来週 <水曜> 15時から & ゼミ")
    assert parse_call("org.freedesktop.Notifications", "Notify", ["Slack", 3, "", "#general", "明日", [], {}, 5000])[1] == ""


def test_parse_gtk_add_notification():
    body = ["com.discordapp.Discord", "n1", {"title": V("佐藤"), "body": V("明日10時から")}]
    assert parse_call("org.gtk.Notifications", "AddNotification", body) == ("Discord", "com.discordapp.Discord", "佐藤", "明日10時から")


def test_parse_ignores_other_calls():
    assert parse_call("org.freedesktop.Notifications", "CloseNotification", [1]) is None
    assert parse_call("org.freedesktop.Notifications", "Notify", ["壊れた呼び出し"]) is None
    assert clean_markup("a <i>b</i> &quot;c&quot;") == 'a b "c"'


def test_buffers_and_drops_duplicates():
    src = DBusSource()
    src.add("discord", "discord", "山田", "15時から")
    src.add("Discord", "com.discordapp.Discord", "山田", "15時から")  # 同じ通知が別の経路でも届いた
    src.add("discord", "discord", "山田", "16時から")
    assert [t.body for t in src._pending] == ["15時から", "16時から"]
    assert len({t.key for t in src._pending}) == 2


async def test_open_without_session_bus(monkeypatch):
    pytest.importorskip("dbus_fast")
    monkeypatch.setattr(linux, "session_bus_address", lambda: None)
    with pytest.raises(RuntimeError, match="セッションバスが見つかりません"):
        await DBusSource().open()


def test_session_bus_address(monkeypatch):
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    assert linux.session_bus_address() == "unix:path=/run/user/1000/bus"


# ---------- 本物のセッションバス（dbus-daemon）で ----------
@pytest.fixture
def session_bus():
    pytest.importorskip("dbus_fast")
    daemon = shutil.which("dbus-daemon")
    if not daemon:
        pytest.skip("dbus-daemon がありません")
    proc = subprocess.Popen([daemon, "--session", "--nofork", "--nopidfile", "--print-address=1"], stdout=subprocess.PIPE, text=True)
    address = proc.stdout.readline().strip()
    try:
        yield address
    finally:
        proc.terminate()
        proc.wait(5)


async def send_notifications(address: str) -> None:
    from dbus_fast import Message, Variant
    from dbus_fast.aio import MessageBus

    sender = await MessageBus(bus_address=address).connect()
    try:
        # 通知サーバーがいなくても、呼び出し自体は受け取れる（返事はエラーになる）
        await sender.call(Message(
            destination="org.freedesktop.Notifications", path="/org/freedesktop/Notifications",
            interface="org.freedesktop.Notifications", member="Notify", signature="susssasa{sv}i",
            body=["discord", 0, "", "⁨山田⁩ (⁨#連絡⁩, ⁨研究室⁩)", "来週水曜15時からゼミです",
                  [], {"desktop-entry": Variant("s", "discord")}, -1]))
        await sender.call(Message(
            destination="org.gtk.Notifications", path="/org/gtk/Notifications",
            interface="org.gtk.Notifications", member="AddNotification", signature="ssa{sv}",
            body=["com.discordapp.Discord", "n1", {"title": Variant("s", "佐藤"), "body": Variant("s", "明日10時からどう？")}]))
        # 通知と関係ない呼び出しは受け取らない
        await sender.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                  interface="org.freedesktop.DBus", member="ListNames"))
    finally:
        sender.disconnect()


async def test_receives_notifications_on_real_session_bus(env, session_bus):
    service, *_ = env
    service.settings.notify_apps = ["Discord"]
    conn = NotificationConnector(service.settings, service, sources=[DBusSource(address=session_bus)])
    await conn.start()
    conn._task.cancel()
    assert conn.status()["state"] == "listening" and conn.status()["backend"] == "dbus"
    await send_notifications(session_bus)
    queued = []
    for _ in range(100):
        queued += await conn.poll_once()
        if len(queued) >= 2:
            break
        await asyncio.sleep(0.05)
    assert [(m.author, m.channel, m.text) for m in queued] == [
        ("山田", "Discord 研究室 #連絡", "来週水曜15時からゼミです"),
        ("佐藤", "Discord DM", "明日10時からどう？"),
    ]
    for job in list(conn._jobs):
        await job
    await conn.stop()


async def test_reconnects_after_session_bus_restart(session_bus):
    src = DBusSource(address=session_bus)
    await src.open()
    src._bus.disconnect()  # セッションバスとの接続が切れた
    await asyncio.sleep(0.1)
    assert await src.fetch() == []  # つなぎ直す
    assert src._bus is not None and src._bus.connected
    await send_notifications(session_bus)
    got = []
    for _ in range(100):
        got += await src.fetch()
        if got:
            break
        await asyncio.sleep(0.05)
    assert got and got[0].app_name == "discord"
    await src.close()
