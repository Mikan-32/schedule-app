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
    """有効な連絡ツールのコネクタを生成する。現段階は Discord のみ（Slack / Zoom は今後追加）。"""
    connectors: list[Connector] = []

    if settings.discord_enabled:
        from .discord_bot import DiscordConnector

        discord_conn = DiscordConnector(settings, service)
        notifier.register(discord_conn)  # 通知は Discord DM にも送る
        connectors.append(discord_conn)
    else:
        log.warning("DISCORD_BOT_TOKEN が未設定のため Discord 連携は無効です（Web UIのみで動作します）")

    # 例: if settings.slack_enabled: connectors.append(SlackConnector(settings, service))
    return connectors


__all__ = ["Connector", "build_connectors"]
