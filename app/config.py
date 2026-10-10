"""アプリ全体の設定。.env または環境変数から読み込む。"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _float(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    return float(value) if value else default


def _id_list(name: str) -> list[int]:
    raw = os.getenv(name, "")
    return [int(x) for x in raw.replace(" ", "").split(",") if x]


@dataclass
class Settings:
    # --- 全般 ---
    timezone: str = field(default_factory=lambda: os.getenv("TIMEZONE", "Asia/Tokyo"))
    db_path: Path = field(default_factory=lambda: BASE_DIR / os.getenv("DB_PATH", "data/schedule.db"))
    host: str = field(default_factory=lambda: os.getenv("HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _int("PORT", 8000))

    # --- ローカルLLM (Ollama) ---
    ollama_base_url: str = field(default_factory=lambda: os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/"))
    # "auto" なら、インストール済みモデルから精度の高い順に自動選択
    ollama_model: str = field(default_factory=lambda: os.getenv("OLLAMA_MODEL", "auto"))
    ollama_vision_model: str = field(default_factory=lambda: os.getenv("OLLAMA_VISION_MODEL", "auto"))
    llm_think: bool = field(default_factory=lambda: _bool("LLM_THINK", False))
    llm_timeout: float = field(default_factory=lambda: _float("LLM_TIMEOUT", 300.0))
    min_confidence: float = field(default_factory=lambda: _float("MIN_CONFIDENCE", 0.5))
    # 日付・時刻らしき表現がないテキストはLLMに送らない（負荷軽減）
    prefilter: bool = field(default_factory=lambda: _bool("PREFILTER", True))
    max_image_side: int = field(default_factory=lambda: _int("MAX_IMAGE_SIDE", 1600))

    # --- 重複判定 ---
    # overlap: 同日かつ時間帯が重なる場合 / same_day: 同日に予定があれば常に確認
    conflict_mode: str = field(default_factory=lambda: os.getenv("CONFLICT_MODE", "overlap"))
    default_duration_min: int = field(default_factory=lambda: _int("DEFAULT_DURATION_MIN", 60))

    # --- Discord ---
    discord_token: str = field(default_factory=lambda: os.getenv("DISCORD_BOT_TOKEN", ""))
    discord_owner_id: int = field(default_factory=lambda: _int("DISCORD_OWNER_ID", 0))
    # 空なら Bot が参加している全チャンネルを監視
    discord_watch_channel_ids: list[int] = field(default_factory=lambda: _id_list("DISCORD_WATCH_CHANNEL_IDS"))
    # false にすると Bot はサーバーのチャンネルを読まず、DM 通知の送信（とBotへのDM/転送の精査）だけを行う
    discord_read_channels: bool = field(default_factory=lambda: _bool("DISCORD_READ_CHANNELS", True))

    # --- Windows 通知 (Bot 不要) ---
    # auto: Windows なら有効 / true / false
    windows_notifications: str = field(default_factory=lambda: os.getenv("WINDOWS_NOTIFICATIONS", "auto").strip().lower())
    # 監視するアプリ（表示名または AppUserModelID の部分一致、カンマ区切り）
    windows_notify_apps: list[str] = field(
        default_factory=lambda: [a.strip() for a in os.getenv("WINDOWS_NOTIFY_APPS", "Discord").split(",") if a.strip()]
    )
    # auto: winrt を試して使えなければ database / winrt / database
    windows_notify_backend: str = field(default_factory=lambda: os.getenv("WINDOWS_NOTIFY_BACKEND", "auto").strip().lower())
    windows_notify_poll_sec: float = field(default_factory=lambda: _float("WINDOWS_NOTIFY_POLL_SEC", 2.0))
    # 起動時に、直近何分以内の通知までさかのぼって精査するか
    windows_notify_backfill_min: int = field(default_factory=lambda: _int("WINDOWS_NOTIFY_BACKFILL_MIN", 30))
    windows_notify_db_path: Optional[Path] = field(
        default_factory=lambda: Path(os.environ["WINDOWS_NOTIFY_DB_PATH"]) if os.getenv("WINDOWS_NOTIFY_DB_PATH") else None
    )

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def discord_enabled(self) -> bool:
        return bool(self.discord_token)

    @property
    def windows_notifications_enabled(self) -> bool:
        if self.windows_notifications == "auto":
            return sys.platform == "win32"
        return self.windows_notifications in {"1", "true", "yes", "on"}


settings = Settings()
