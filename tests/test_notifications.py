"""PC の通知（Windows・macOS・Linux 共通）のテスト: 設定の名前・読み取り方式の選び方・Discord の通知の読み方・確認コマンド。"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import Settings
from app.connectors import notifications
from app.connectors.linux_notifications import DBusSource
from app.connectors.macos_notifications import MacDatabaseSource
from app.connectors.notifications import (
    NotificationConnector,
    NotificationSource,
    ToastNotification,
    default_sources,
    parse_discord_title,
    to_incoming,
)
from app.connectors.windows_notifications import DatabaseSource, WinRTSource

NOW = datetime.now(timezone.utc)


def test_notify_settings_accept_old_names(monkeypatch):
    for key in ("NOTIFICATIONS", "NOTIFY_APPS", "NOTIFY_BACKEND", "NOTIFY_POLL_SEC", "NOTIFY_BACKFILL_MIN", "NOTIFY_DB_PATH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("WINDOWS_NOTIFICATIONS", "true")
    monkeypatch.setenv("WINDOWS_NOTIFY_APPS", "Discord, Slack")
    monkeypatch.setenv("WINDOWS_NOTIFY_BACKEND", "database")
    monkeypatch.setenv("WINDOWS_NOTIFY_POLL_SEC", "5")
    monkeypatch.setenv("WINDOWS_NOTIFY_BACKFILL_MIN", "10")
    monkeypatch.setenv("WINDOWS_NOTIFY_DB_PATH", "/x/wpndatabase.db")
    s = Settings()
    assert (s.notifications, s.notify_apps, s.notify_backend, s.notify_poll_sec, s.notify_backfill_min) == (
        "true", ["Discord", "Slack"], "database", 5.0, 10)
    assert s.notify_db_path == Path("/x/wpndatabase.db") and s.notifications_enabled
    # 新しい名前があればそちらを使う
    monkeypatch.setenv("NOTIFY_APPS", "LINE")
    monkeypatch.setenv("NOTIFICATIONS", "false")
    s = Settings()
    assert s.notify_apps == ["LINE"] and not s.notifications_enabled


def test_notifications_auto_per_os(monkeypatch):
    for name, enabled in (("win32", True), ("darwin", True), ("linux", True), ("freebsd14", False)):
        monkeypatch.setattr(sys, "platform", name)
        assert Settings(notifications="auto").notifications_enabled is enabled


def test_default_sources_per_os(monkeypatch):
    kinds = lambda backend="auto": [type(x) for x in default_sources(Settings(notify_backend=backend))]  # noqa: E731
    monkeypatch.setattr(sys, "platform", "win32")
    assert kinds() == [WinRTSource, DatabaseSource]
    assert kinds("database") == [DatabaseSource] and kinds("winrt") == [WinRTSource]
    monkeypatch.setattr(sys, "platform", "darwin")
    assert kinds() == [MacDatabaseSource]
    monkeypatch.setattr(sys, "platform", "linux")
    assert kinds() == [DBusSource]
    monkeypatch.setattr(sys, "platform", "freebsd14")
    assert kinds() == []


async def test_connector_on_unsupported_os(env, monkeypatch):
    service, *_ = env
    monkeypatch.setattr(sys, "platform", "freebsd14")
    conn = NotificationConnector(service.settings, service)
    await conn.start()
    status = conn.status()
    assert status["state"] == "error" and "対応していません" in status["error"] and status["os"] == "other"


def test_parse_discord_title_with_subtitle():
    # macOS で、チャンネルとサーバーがサブタイトルに分かれている場合
    assert parse_discord_title("山田", "#連絡, 研究室") == ("山田", "研究室 #連絡")
    assert parse_discord_title("⁨山田⁩", "⁨#連絡⁩, ⁨研究室⁩") == ("山田", "研究室 #連絡")
    # サブタイトルの形が分からないときは DM と決めつけない（DM は確認なしで登録されるため）
    assert parse_discord_title("山田", "研究室") == ("山田", "研究室")
    assert parse_discord_title("山田 (#連絡, 研究室)", "何か") == ("山田", "研究室 #連絡")
    assert parse_discord_title("佐藤", "") == ("佐藤", "DM")


def test_to_incoming_with_subtitle():
    slack = ToastNotification("s", "Slack", "com.tinyspeck.slackmacgap", ["#general", "明日10時から"], NOW, subtitle="田中")
    m = to_incoming(slack, "Slack")
    assert (m.author, m.channel, m.text, m.source) == ("", "Slack: #general", "田中\n明日10時から", "winnotify")
    discord = ToastNotification("d", "Discord", "com.hnc.Discord", ["山田", "15時から"], NOW, subtitle="#連絡, 研究室")
    m = to_incoming(discord, "Discord")
    assert (m.author, m.channel, m.text) == ("山田", "Discord 研究室 #連絡", "15時から")


class ListSource(NotificationSource):
    name = "list"

    def __init__(self, toasts=None, fail=False):
        self.toasts = list(toasts or [])
        self.fail = fail
        self.closed = False

    async def open(self):
        if self.fail:
            raise PermissionError("許可がありません")

    async def fetch(self):
        out, self.toasts = self.toasts, []
        return out

    async def close(self):
        self.closed = True


async def test_check_command(env, monkeypatch, capsys):
    service, *_ = env
    service.settings.notify_apps = ["Discord"]
    good = ListSource([
        ToastNotification("a", "Discord", "com.hnc.Discord", ["山田 (#連絡, 研究室)", "来週水曜15時から"], NOW),
        ToastNotification("b", "メール", "com.apple.mail", ["お知らせ", "セール"], NOW - timedelta(minutes=1)),
    ])
    bad = ListSource(fail=True)
    bad.name = "winrt"
    monkeypatch.setattr(notifications, "default_sources", lambda settings: [bad, good])
    assert await notifications.check(0, service.settings) == 0
    out = capsys.readouterr().out
    assert "[NG] winrt: 許可がありません" in out and "[OK] list" in out
    assert "◎" in out and "送信者: 山田 / チャンネル: Discord 研究室 #連絡" in out
    assert "・" in out and "セール" in out  # 対象外のアプリの通知も表示する（NOTIFY_APPS の設定の参考に）
    assert good.closed
    monkeypatch.setattr(notifications, "default_sources", lambda settings: [bad])
    assert await notifications.check(0, service.settings) == 1


async def test_connector_closes_source(env):
    service, *_ = env
    src = ListSource()
    conn = NotificationConnector(service.settings, service, sources=[src])
    await conn.start()
    assert conn.status()["backend"] == "list" and conn.label.endswith("の通知")
    await conn.stop()
    assert src.closed and conn.status()["state"] == "stopped"
