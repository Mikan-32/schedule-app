"""Windows の通知を直接読み取るコネクタ（Bot 不要）。

Discord / Slack / Teams / LINE などのデスクトップアプリが出したトースト通知を読み取り、
本文を精査して予定を登録する。

読み取り方式（WINDOWS_NOTIFY_BACKEND）:
  - winrt    : Windows 公式の UserNotificationListener API（PyWinRT 経由）。初回にアクセス許可が必要
  - database : 通知センターのデータベース (wpndatabase.db) を読み取り専用で参照する
  - auto     : winrt を試し、使えなければ database に切り替える（既定）

制約: 通知に表示される文章しか取れない（画像の中身は不可・長文は途中で切れることがある）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import xml.etree.ElementTree as ET
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from ..llm import LLMUnavailable
from ..models import IncomingMessage
from .base import Connector

if TYPE_CHECKING:
    from ..config import Settings
    from ..service import ScheduleService

log = logging.getLogger(__name__)

_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


@dataclass
class ToastNotification:
    key: str  # 一意キー（重複処理防止用）
    app_name: str  # 表示名 (例: Discord)。取れない場合は空
    app_id: str  # AppUserModelID (例: com.squirrel.Discord.Discord)
    texts: list[str]  # トーストのテキスト要素（先頭がタイトル）
    created_at: datetime  # tz-aware

    @property
    def title(self) -> str:
        return self.texts[0] if self.texts else ""

    @property
    def body(self) -> str:
        return "\n".join(self.texts[1:])


# ---------- 解析ヘルパ ----------
def parse_toast_xml(payload: bytes | str) -> list[str]:
    """トースト XML から <text> 要素の文字列を順に取り出す。"""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")
    payload = payload.strip().lstrip("﻿")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return []
    texts = []
    for binding in root.iter("binding"):
        for el in binding.iter("text"):
            value = "".join(el.itertext()).strip()
            if value:
                texts.append(value)
        if texts:
            break
    return texts


def filetime_to_datetime(value: int) -> datetime:
    return _FILETIME_EPOCH + timedelta(microseconds=int(value) // 10)


_DISCORD_TITLE = re.compile(r"^(?P<author>.+?) \((?P<channel>#[^,]+), (?P<server>.+)\)$")


def parse_discord_title(title: str) -> tuple[str, str]:
    """Discord の通知タイトル「ユーザー名 (#チャンネル, サーバー名)」を (送信者, チャンネル) に分解する。"""
    m = _DISCORD_TITLE.match(title.strip())
    if m:
        return m["author"], f"{m['server']} {m['channel']}"
    return title.strip(), "DM"


def match_app(toast: ToastNotification, targets: list[str]) -> Optional[str]:
    """監視対象アプリならその名前を返す（表示名・AppUserModelID の部分一致、大文字小文字無視）。"""
    hay = f"{toast.app_name} {toast.app_id}".lower()
    for t in targets:
        if t.lower() in hay:
            return t
    return None


def to_incoming(toast: ToastNotification, app_label: str) -> IncomingMessage:
    title, body = toast.title, toast.body
    if app_label.lower() == "discord":
        author, channel = parse_discord_title(title)
        channel = f"Discord {channel}"
    else:
        author, channel = "", f"{toast.app_name or app_label}: {title}"
    return IncomingMessage(
        source="winnotify",
        message_id=toast.key,
        text=body or title,
        sent_at=toast.created_at,
        author=author,
        channel=channel,
    )


# ---------- 読み取り方式 ----------
class NotificationSource(ABC):
    name = "base"

    @abstractmethod
    async def open(self) -> None:
        """使えない場合は例外を投げる。"""

    @abstractmethod
    async def fetch(self) -> list[ToastNotification]:
        """現在の（または前回以降の）トースト通知を返す。"""


class WinRTSource(NotificationSource):
    """UserNotificationListener (PyWinRT)。"""

    name = "winrt"

    async def open(self) -> None:
        import winrt.windows.ui.notifications as notifications  # type: ignore
        import winrt.windows.ui.notifications.management as management  # type: ignore

        self._kinds = notifications.NotificationKinds.TOAST
        self._listener = management.UserNotificationListener.current
        status = await self._listener.request_access_async()
        if status != management.UserNotificationListenerAccessStatus.ALLOWED:
            raise PermissionError(
                "通知へのアクセスが許可されていません（設定 > プライバシーとセキュリティ > 通知 を確認してください）"
            )

    async def fetch(self) -> list[ToastNotification]:
        result = []
        for t in list(await self._listener.get_notifications_async(self._kinds)):
            try:
                app_name = t.app_info.display_info.display_name or ""
                app_id = t.app_info.app_user_model_id or ""
            except Exception:
                app_name = app_id = ""
            texts: list[str] = []
            try:
                binding = t.notification.visual.get_binding("ToastGeneric")
                if binding:
                    texts = [e.text for e in binding.get_text_elements() if e.text]
            except Exception:
                pass
            created = t.creation_time if isinstance(t.creation_time, datetime) else datetime.now(timezone.utc)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            result.append(
                ToastNotification(
                    key=f"winrt-{t.id}-{int(created.timestamp())}",
                    app_name=app_name,
                    app_id=app_id,
                    texts=texts,
                    created_at=created,
                )
            )
        return result


def default_db_path() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Notifications" / "wpndatabase.db"


class DatabaseSource(NotificationSource):
    """通知センターのデータベース (wpndatabase.db) を読み取り専用で参照する。"""

    name = "database"
    QUERY = """
        SELECT n."Order", n.Payload, n.ArrivalTime, h.PrimaryId
        FROM Notification n JOIN NotificationHandler h ON n.HandlerId = h.RecordId
        WHERE n."Order" > ? AND lower(n.Type) = 'toast'
        ORDER BY n."Order"
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else default_db_path()
        self._last_order = 0

    async def open(self) -> None:
        if not self.path.exists():
            raise FileNotFoundError(f"通知データベースが見つかりません: {self.path}")
        await asyncio.to_thread(self._query, 2**62)  # 読めるかだけ確認（結果は空）

    def _connect_direct(self) -> sqlite3.Connection:
        return sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)

    def _query(self, last_order: int) -> list[tuple]:
        try:
            conn = self._connect_direct()
            try:
                return conn.execute(self.QUERY, (last_order,)).fetchall()
            finally:
                conn.close()
        except sqlite3.Error:
            # 使用中で開けない場合は、WAL ごと一時フォルダにコピーして読む
            with tempfile.TemporaryDirectory() as tmp:
                for suffix in ("", "-wal", "-shm"):
                    src = Path(str(self.path) + suffix)
                    if src.exists():
                        shutil.copy2(src, Path(tmp) / (self.path.name + suffix))
                conn = sqlite3.connect(Path(tmp) / self.path.name)
                try:
                    return conn.execute(self.QUERY, (last_order,)).fetchall()
                finally:
                    conn.close()

    async def fetch(self) -> list[ToastNotification]:
        rows = await asyncio.to_thread(self._query, self._last_order)
        result = []
        for order, payload, arrival, app_id in rows:
            self._last_order = max(self._last_order, order)
            if payload is None:
                continue
            result.append(
                ToastNotification(
                    key=f"db-{order}",
                    app_name="",
                    app_id=app_id or "",
                    texts=parse_toast_xml(payload),
                    created_at=filetime_to_datetime(arrival) if arrival else datetime.now(timezone.utc),
                )
            )
        return result


# ---------- コネクタ ----------
class WindowsNotificationConnector(Connector):
    name = "windows_notifications"

    def __init__(self, settings: "Settings", service: "ScheduleService", sources: Optional[list[NotificationSource]] = None):
        self.settings = settings
        self.service = service
        self.apps = settings.windows_notify_apps
        self._candidates = sources
        self.source: Optional[NotificationSource] = None
        self.state = "stopped"
        self.error = ""
        self._seen: set[str] = set()
        self._first_poll = True
        self._task: Optional[asyncio.Task] = None
        self._jobs: set[asyncio.Task] = set()

    def _default_sources(self) -> list[NotificationSource]:
        backend = self.settings.windows_notify_backend
        db = DatabaseSource(self.settings.windows_notify_db_path)
        if backend == "winrt":
            return [WinRTSource()]
        if backend == "database":
            return [db]
        return [WinRTSource(), db]

    # ---------- lifecycle ----------
    async def start(self) -> None:
        self.state = "starting"
        errors = []
        for src in self._candidates or self._default_sources():
            try:
                await src.open()
                self.source = src
                break
            except Exception as e:
                errors.append(f"{src.name}: {e}")
                log.warning("Windows通知 (%s) は使用できません: %s", src.name, e)
        if self.source is None:
            self.state, self.error = "error", " / ".join(errors)
            log.error("Windows通知を読み取れません: %s", self.error)
            return
        self.state, self.error = "listening", ""
        log.info("Windows通知の監視を開始しました（方式: %s, 対象: %s）", self.source.name, ", ".join(self.apps))
        self._task = asyncio.create_task(self._loop(), name="windows-notifications")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        for job in list(self._jobs):
            job.cancel()
        self.state = "stopped"

    def status(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "error": self.error,
            "backend": self.source.name if self.source else None,
            "apps": self.apps,
        }

    # ---------- 監視 ----------
    async def _loop(self) -> None:
        while True:
            try:
                await self.poll_once()
                if self.state == "error":
                    self.state, self.error = "listening", ""
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.state, self.error = "error", f"読み取りに失敗: {e}"
                log.warning("Windows通知の読み取りに失敗: %s", e)
            await asyncio.sleep(self.settings.windows_notify_poll_sec)

    async def poll_once(self) -> list[IncomingMessage]:
        """新しい通知を取得して精査に回す。精査に回したメッセージを返す。"""
        toasts = await self.source.fetch()
        now = datetime.now(timezone.utc)
        backfill = timedelta(minutes=self.settings.windows_notify_backfill_min)
        queued = []
        for toast in toasts:
            if toast.key in self._seen:
                continue
            # 起動直後は、直近 N 分以内の通知だけを対象にする
            if self._first_poll and now - toast.created_at > backfill:
                continue
            app = match_app(toast, self.apps)
            if not app or not toast.texts:
                continue
            msg = to_incoming(toast, app)
            queued.append(msg)
            job = asyncio.create_task(self._handle(msg))
            self._jobs.add(job)
            job.add_done_callback(self._jobs.discard)
        # WinRT は「現在ある通知」を毎回返すので、見たキーを覚えておく（消えた通知は忘れてよい）
        if self.source.name == "winrt":
            self._seen = {t.key for t in toasts}
        self._first_poll = False
        return queued

    async def _handle(self, msg: IncomingMessage) -> None:
        try:
            await self.service.handle_message(msg)
        except LLMUnavailable as e:
            log.error("ローカルLLMに接続できません: %s", e)
        except Exception:
            log.exception("通知の精査に失敗しました")
