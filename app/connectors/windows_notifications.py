"""Windows の通知の読み取り方式（app/connectors/notifications.py の NotificationConnector が使う）。

  - winrt    : Windows 公式の UserNotificationListener API（PyWinRT 経由）。初回にアクセス許可が必要
  - database : 通知センターのデータベース (wpndatabase.db) を読み取り専用で参照する

NOTIFY_BACKEND=auto（既定）なら winrt を試し、使えなければ database に切り替える。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .notifications import NotificationSource, ToastNotification

_FILETIME_EPOCH = datetime(1601, 1, 1, tzinfo=timezone.utc)


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


# ---------- 読み取り方式 ----------
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
