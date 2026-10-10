from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .base import Connector

if TYPE_CHECKING:
    from ..config import Settings
    from ..notifier import Notifier
    from ..service import ScheduleService

log = logging.getLogger(__name__)


def build_connectors(settings: "Settings", service: "ScheduleService", notifier: "Notifier") -> list[Connector]:
    """有効な連絡ツールのコネクタを生成する。"""
    connectors: list[Connector] = []

    # Windows の通知を直接読み取る（Bot 不要。WINDOWS_NOTIFY_APPS で Slack 等も対象にできる）
    if settings.windows_notifications_enabled:
        from .windows_notifications import WindowsNotificationConnector

        connectors.append(WindowsNotificationConnector(settings, service))

    # Discord Bot（チャンネルの精査 + 画像の精査 + DM 通知・確認ボタン）
    if settings.discord_enabled:
        from .discord_bot import DiscordConnector

        discord_conn = DiscordConnector(settings, service)
        notifier.register(discord_conn)  # 通知は Discord DM にも送る
        connectors.append(discord_conn)
    else:
        log.info("DISCORD_BOT_TOKEN が未設定のため Discord Bot は無効です")

    if not connectors:
        log.warning("有効な連絡ツールがありません（Web UI の「メッセージを解析」のみ利用できます）")

    # 例: if settings.slack_enabled: connectors.append(SlackConnector(settings, service))
    return connectors


__all__ = ["Connector", "build_connectors"]
