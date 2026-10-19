"""PC の通知を直接読み取るコネクタ（Bot 不要）。Windows・macOS・Linux に対応。

Discord / Slack / Teams / LINE などのデスクトップアプリが出した通知を読み取り、
本文を精査して予定を登録する。

読み取り方式（NOTIFY_BACKEND。auto なら OS ごとに上から順に試す）:
  Windows: winrt    … Windows 公式の UserNotificationListener API（PyWinRT 経由）。初回にアクセス許可が必要
           database … 通知センターのデータベース (wpndatabase.db) を読み取り専用で参照する
  macOS  : database … 通知センターのデータベースを読み取り専用で参照する（「フルディスクアクセス」の許可が必要）
  Linux  : dbus     … セッションバスで、アプリが通知を出す呼び出し（org.freedesktop.Notifications.Notify など）を受け取る
それぞれの実装は windows_notifications.py / macos_notifications.py / linux_notifications.py。

制約: 通知に表示される文章しか取れない（画像の中身は不可・長文は途中で切れることがある）。

読み取れるかの確認（アプリのフォルダで）:
  python -m app.connectors.notifications
  使える読み取り方式・最近の通知を表示し、30 秒間は新しく届いた通知も表示する（--wait で秒数を変えられる）。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import re
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

from ..config import os_kind
from ..llm import LLMUnavailable
from ..models import IncomingMessage
from .base import Connector

if TYPE_CHECKING:
    from ..config import Settings
    from ..service import ScheduleService

log = logging.getLogger(__name__)

OS_LABELS = {"windows": "Windows", "macos": "Mac", "linux": "Linux"}


@dataclass
class ToastNotification:
    key: str  # 一意キー（重複処理防止用）
    app_name: str  # 表示名 (例: Discord)。取れない場合は空
    app_id: str  # アプリID（Windows: AppUserModelID、macOS: バンドルID、Linux: desktop-entry など）
    texts: list[str]  # 通知のテキスト（先頭がタイトル、残りが本文）
    created_at: datetime  # tz-aware
    subtitle: str = ""  # サブタイトル（macOS のみ）

    @property
    def title(self) -> str:
        return self.texts[0] if self.texts else ""

    @property
    def body(self) -> str:
        return "\n".join(self.texts[1:])


# ---------- 解析ヘルパ ----------
_DISCORD_TITLE = re.compile(r"^(?P<author>.+?) \((?P<channel>#[^,]+), (?P<server>.+)\)$")
_DISCORD_SUBTITLE = re.compile(r"^(?P<channel>#[^,]+), (?P<server>.+)$")
# 見えない制御文字（双方向テキストの分離・ゼロ幅文字など）。Discord は名前やチャンネル名を U+2068…U+2069 で囲んで通知する
_INVISIBLE = re.compile("[​-‏‪-‮⁦-⁩﻿]")


def strip_invisible(text: str) -> str:
    return _INVISIBLE.sub("", text or "")


def parse_discord_title(title: str, subtitle: str = "") -> tuple[str, str]:
    """Discord の通知タイトル「ユーザー名 (#チャンネル, サーバー名)」を (送信者, チャンネル) に分解する。

    サーバーのチャンネル以外（チャンネルが付いていないもの）は DM とみなす。
    macOS でチャンネルとサーバーがサブタイトル「#チャンネル, サーバー名」に分かれている場合にも対応する。
    サブタイトルがあるのに形が分からないときは、DM と決めつけずにサブタイトルをチャンネルとして扱う
    （DM は「自分宛て」になり確認なしで登録されるため、迷ったら DM にしない）。
    """
    title = strip_invisible(title).strip()
    m = _DISCORD_TITLE.match(title)
    if m:
        return m["author"], f"{m['server']} {m['channel']}"
    subtitle = strip_invisible(subtitle).strip()
    if subtitle:
        s = _DISCORD_SUBTITLE.match(subtitle)
        channel = f"{s['server']} {s['channel']}" if s else subtitle
        return title, channel
    return title, "DM"


def match_app(toast: ToastNotification, targets: list[str]) -> Optional[str]:
    """監視対象アプリならその名前を返す（表示名・アプリID の部分一致、大文字小文字無視）。"""
    hay = f"{toast.app_name} {toast.app_id}".lower()
    for t in targets:
        if t.lower() in hay:
            return t
    return None


def to_incoming(toast: ToastNotification, app_label: str) -> IncomingMessage:
    title, body = strip_invisible(toast.title), strip_invisible(toast.body)
    subtitle = strip_invisible(toast.subtitle).strip()
    if app_label.lower() == "discord":
        author, channel = parse_discord_title(title, subtitle)
        channel = f"Discord {channel}"
    else:
        author, channel = "", f"{toast.app_name or app_label}: {title}"
        if subtitle:
            body = f"{subtitle}\n{body}".strip()
    return IncomingMessage(
        source="winnotify",  # PC の通知（Windows 以外も同じ。以前からの名前）
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
        """現在の（または前回以降の）通知を返す。"""

    async def close(self) -> None:
        """後片付け（必要な方式だけ）。"""


def default_sources(settings: "Settings") -> list[NotificationSource]:
    """この OS で試す読み取り方式を、試す順に返す（対応していない OS なら空）。"""
    kind = os_kind()
    backend = settings.notify_backend
    if kind == "windows":
        from .windows_notifications import DatabaseSource, WinRTSource

        if backend == "winrt":
            return [WinRTSource()]
        if backend == "database":
            return [DatabaseSource(settings.notify_db_path)]
        return [WinRTSource(), DatabaseSource(settings.notify_db_path)]
    if kind == "macos":
        from .macos_notifications import MacDatabaseSource

        return [MacDatabaseSource(settings.notify_db_path)]
    if kind == "linux":
        from .linux_notifications import DBusSource

        return [DBusSource()]
    return []


# ---------- コネクタ ----------
class NotificationConnector(Connector):
    name = "notifications"
    RETRY_SEC = 60.0  # 読み取りを始められなかったとき（Mac の許可がまだ・セッションバスがまだ等）に試し直す間隔

    def __init__(self, settings: "Settings", service: "ScheduleService", sources: Optional[list[NotificationSource]] = None):
        self.settings = settings
        self.service = service
        self.apps = settings.notify_apps
        self.os = os_kind()
        self._candidates = sources
        self.source: Optional[NotificationSource] = None
        self.state = "stopped"
        self.error = ""
        self._seen: set[str] = set()
        self._first_poll = True
        self._task: Optional[asyncio.Task] = None
        self._jobs: set[asyncio.Task] = set()

    @property
    def label(self) -> str:
        return f"{OS_LABELS.get(self.os, 'PC')} の通知"

    # ---------- lifecycle ----------
    async def start(self) -> None:
        self.state = "starting"
        candidates = self._candidates if self._candidates is not None else default_sources(self.settings)
        if not candidates:
            self.state, self.error = "error", "この OS の通知の読み取りには対応していません"
            log.error("%sを読み取れません: %s", self.label, self.error)
            return
        if await self._open(candidates):
            self._task = asyncio.create_task(self._loop(), name="notifications")
        else:
            # 許可をもらった・ログインしたなど、あとから使えるようになることがあるので、ときどき試し直す
            self._task = asyncio.create_task(self._retry_open(), name="notifications-open")

    async def _open(self, candidates: list[NotificationSource]) -> bool:
        errors = []
        for src in candidates:
            try:
                await src.open()
            except Exception as e:
                errors.append(f"{src.name}: {e}")
                continue
            self.source = src
            self.state, self.error = "listening", ""
            log.info("%sの監視を開始しました（方式: %s, 対象: %s）", self.label, src.name, ", ".join(self.apps))
            return True
        error = " / ".join(errors)
        if error != self.error:  # 同じ理由で失敗し続けるときはログに 1 回だけ出す
            log.error("%sを読み取れません: %s", self.label, error)
        self.state, self.error = "error", error
        return False

    async def _retry_open(self) -> None:
        while True:
            await asyncio.sleep(self.RETRY_SEC)
            candidates = self._candidates if self._candidates is not None else default_sources(self.settings)
            if await self._open(candidates):
                await self._loop()
                return

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        for job in list(self._jobs):
            job.cancel()
        if self.source:
            try:
                await self.source.close()
            except Exception:
                log.debug("通知の読み取りの後片付けに失敗しました", exc_info=True)
        self.state = "stopped"

    def status(self) -> dict:
        return {
            "name": self.name,
            "os": self.os,
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
                error = f"読み取りに失敗: {e}"
                if error != self.error:  # 同じエラーが続くときはログに 1 回だけ出す
                    log.warning("%sの読み取りに失敗: %s", self.label, e)
                self.state, self.error = "error", error
            await asyncio.sleep(self.settings.notify_poll_sec)

    async def poll_once(self) -> list[IncomingMessage]:
        """新しい通知を取得して精査に回す。精査に回したメッセージを返す。"""
        toasts = await self.source.fetch()
        now = datetime.now(timezone.utc)
        backfill = timedelta(minutes=self.settings.notify_backfill_min)
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


# ---------- 読み取れるかの確認（python -m app.connectors.notifications） ----------
def _print_toast(toast: ToastNotification, settings: "Settings") -> None:
    app = match_app(toast, settings.notify_apps)
    when = toast.created_at.astimezone(settings.tz).strftime("%m/%d %H:%M:%S")
    head = f"{'◎' if app else '・'} {when} [{toast.app_name or '?'} / {toast.app_id or '?'}] {toast.title}"
    if toast.subtitle:
        head += f" ／ {toast.subtitle}"
    print(head)
    body = toast.body.replace("\n", " ")
    if body:
        print(f"      {body[:100]}")
    if app:
        msg = to_incoming(toast, app)
        print(f"      → 精査の対象（送信者: {msg.author or '-'} / チャンネル: {msg.channel}）")


async def check(wait: float, settings: Optional["Settings"] = None) -> int:
    from ..config import settings as default_settings

    settings = settings or default_settings
    kind = os_kind()
    print(f"OS: {OS_LABELS.get(kind, sys.platform)}（{sys.platform}） / 対象アプリ（NOTIFY_APPS）: {', '.join(settings.notify_apps)}")
    source = None
    for src in default_sources(settings):
        try:
            await src.open()
        except Exception as e:
            print(f"[NG] {src.name}: {e}")
            continue
        print(f"[OK] {src.name} で読み取れます")
        source = src
        break
    if source is None:
        print("この PC では通知を読み取れません（上の理由を確認してください）" if kind != "other" else "この OS の通知の読み取りには対応していません")
        return 1
    try:
        toasts = await source.fetch()
        if toasts:
            print(f"最近の通知（新しい順に最大 10 件 / 全 {len(toasts)} 件。◎ は精査の対象）:")
            for t in sorted(toasts, key=lambda t: t.created_at, reverse=True)[:10]:
                _print_toast(t, settings)
        if wait > 0:
            print(f"{wait:.0f} 秒間、新しく届いた通知を表示します（Discord などで自分にメッセージを送ってみてください。Ctrl+C で終了）")
            loop = asyncio.get_running_loop()
            end = loop.time() + wait
            while loop.time() < end:
                await asyncio.sleep(1)
                for t in await source.fetch():
                    _print_toast(t, settings)
    finally:
        await source.close()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.connectors.notifications", description="PC の通知を読み取れるか確かめる")
    parser.add_argument("--wait", type=float, default=30, help="新しく届いた通知を表示する秒数（0 で待たない。既定 30）")
    parser.add_argument("--check", action="store_true", help=argparse.SUPPRESS)  # 書いても書かなくても同じ
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING)
    try:
        return asyncio.run(check(args.wait))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    # python -m で実行すると、このファイルは __main__ として読み込まれるので、パッケージのほうの main を呼ぶ
    from app.connectors.notifications import main as _main

    sys.exit(_main())
