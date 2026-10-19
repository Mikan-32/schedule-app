"""Linux のデスクトップ通知を読み取る（NotificationConnector の読み取り方式 dbus）。

アプリが通知を出すときの D-Bus の呼び出しを、セッションバスの監視機能（BecomeMonitor）で受け取る。
  - org.freedesktop.Notifications.Notify … ほとんどのアプリ・デスクトップ環境（Electron 製の Discord / Slack もこれ）
  - org.gtk.Notifications.AddNotification … GNOME の GTK アプリ（GNOME では Flatpak のアプリもここを通る）
自分のセッションバスなので特別な権限は不要。デスクトップ環境（GNOME・KDE など）にログインしている必要がある。
古い dbus-daemon で監視機能が使えないときは、eavesdrop（盗聴）指定の AddMatch で受け取る。
dbus-fast（requirements.txt。Linux のみ）を使う。
"""
from __future__ import annotations

import html
import itertools
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .notifications import NotificationSource, ToastNotification

log = logging.getLogger(__name__)

FREEDESKTOP = ("org.freedesktop.Notifications", "Notify")
GTK = ("org.gtk.Notifications", "AddNotification")
MATCH_RULES = [
    "type='method_call',interface='org.freedesktop.Notifications',member='Notify'",
    "type='method_call',interface='org.gtk.Notifications',member='AddNotification'",
]
# 同じ通知が別の経路（Flatpak の通知ポータル経由など）でも届いたときは 1 回だけ扱う
DEDUPE_SEC = 10.0
MAX_PENDING = 500
_TAG = re.compile(r"<[^>]*>")


def session_bus_address() -> Optional[str]:
    """セッションバスのアドレス。環境変数がなければ systemd の既定の場所（/run/user/<uid>/bus）。"""
    address = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    if address:
        return address
    path = Path(f"/run/user/{os.getuid()}/bus")
    return f"unix:path={path}" if path.exists() else None


def clean_markup(text: str) -> str:
    """通知の本文のマークアップ（<b> など）と文字参照（&amp; など）を普通の文字にする。"""
    return html.unescape(_TAG.sub("", text or ""))


def _value(v: Any) -> Any:
    """dbus-fast の Variant なら中身を返す。"""
    return getattr(v, "value", v)


def parse_call(interface: str, member: str, body: list) -> Optional[tuple[str, str, str, str]]:
    """通知を出す呼び出しの引数から (アプリ名, アプリID, タイトル, 本文) を取り出す。対象外・形が違うなら None。"""
    try:
        if (interface, member) == FREEDESKTOP:
            # Notify(app_name, replaces_id, app_icon, summary, body, actions, hints, expire_timeout)
            app_name, _replaces, _icon, summary, text, _actions, hints = body[:7]
            app_id = str(_value((hints or {}).get("desktop-entry", "")) or "")
            return str(app_name or ""), app_id, str(summary or ""), clean_markup(str(text or ""))
        if (interface, member) == GTK:
            # AddNotification(app_id, id, notification: a{sv})
            app_id, _nid, notification = body[:3]
            title = str(_value(notification.get("title", "")) or "")
            text = str(_value(notification.get("body", "")) or "")
            return str(app_id).rsplit(".", 1)[-1], str(app_id), title, text
    except (ValueError, TypeError, AttributeError):
        return None
    return None


class DBusSource(NotificationSource):
    """セッションバスの通知の呼び出しを受け取る。届いたものを fetch() でまとめて返す。"""

    name = "dbus"

    def __init__(self, address: Optional[str] = None):
        self._address = address
        self._bus: Any = None
        self._pending: list[ToastNotification] = []
        self._recent: dict[tuple, float] = {}
        self._counter = itertools.count(1)

    async def open(self) -> None:
        try:
            from dbus_fast import Message, MessageType
            from dbus_fast.aio import MessageBus
        except ImportError as e:
            raise RuntimeError("dbus-fast が入っていません。pip install -r requirements.txt をやり直してください") from e
        address = self._address or session_bus_address()
        if not address:
            raise RuntimeError("セッションバスが見つかりません（デスクトップにログインした状態でアプリを起動してください）")
        try:
            bus = await MessageBus(bus_address=address).connect()
        except Exception as e:
            raise RuntimeError(f"セッションバスに接続できません（{address}）: {e}") from e

        def bus_call(interface: str, member: str, signature: str, body: list):
            return bus.call(
                Message(
                    destination="org.freedesktop.DBus",
                    path="/org/freedesktop/DBus",
                    interface=interface,
                    member=member,
                    signature=signature,
                    body=body,
                )
            )

        reply = await bus_call("org.freedesktop.DBus.Monitoring", "BecomeMonitor", "asu", [MATCH_RULES, 0])
        if reply.message_type == MessageType.ERROR:
            # 監視機能が使えない（古い dbus-daemon など）→ eavesdrop 指定の AddMatch で受け取る
            log.info("D-Bus の監視機能が使えないため eavesdrop で受け取ります: %s", reply.error_name)
            for rule in MATCH_RULES:
                r = await bus_call("org.freedesktop.DBus", "AddMatch", "s", [rule + ",eavesdrop='true'"])
                if r.message_type == MessageType.ERROR:
                    bus.disconnect()
                    raise RuntimeError(f"通知の呼び出しを受け取れません: {reply.error_name} / {r.error_name}")
        self._message_type = MessageType
        bus.add_message_handler(self._on_message)
        self._bus = bus

    def _on_message(self, msg: Any) -> bool:
        try:
            if msg.message_type == self._message_type.METHOD_CALL:
                parsed = parse_call(msg.interface or "", msg.member or "", list(msg.body or []))
                if parsed:
                    self.add(*parsed)
        except Exception:
            log.exception("通知の読み取りに失敗しました")
        return True  # 返事はしない（監視用の接続は送信できない。ほかのアプリあての呼び出しにも答えない）

    def add(self, app_name: str, app_id: str, title: str, body: str) -> None:
        now = time.monotonic()
        self._recent = {k: t for k, t in self._recent.items() if now - t < DEDUPE_SEC}
        key = (app_name.lower(), title, body)
        if key in self._recent:
            return
        self._recent[key] = now
        self._pending.append(
            ToastNotification(
                key=f"dbus-{time.time_ns()}-{next(self._counter)}",
                app_name=app_name,
                app_id=app_id,
                texts=[title] + ([body] if body else []),
                created_at=datetime.now(timezone.utc),
            )
        )
        del self._pending[:-MAX_PENDING]

    async def fetch(self) -> list[ToastNotification]:
        if self._bus is None or not self._bus.connected:
            # ログアウト・セッションバスの再起動など。つなぎ直す（失敗したら例外。次の確認でまた試す）
            if self._bus is not None:
                log.warning("セッションバスとの接続が切れたため、つなぎ直します")
                self._bus = None
            await self.open()
        out, self._pending = self._pending, []
        return out

    async def close(self) -> None:
        if self._bus is not None:
            self._bus.disconnect()
            self._bus = None
