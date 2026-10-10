"""Discord コネクタ。

- Bot が参加しているサーバーのチャンネル（DISCORD_WATCH_CHANNEL_IDS で絞り込み可）と、
  オーナーから Bot への DM（転送メッセージ含む）を精査する
- 予定追加・重複確認の通知はオーナーに DM で送る。重複時は「追加する / 追加しない」ボタン付き
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Optional

import discord

from ..formatting import format_time_range
from ..llm import LLMUnavailable
from ..models import Event, ImageInput, IncomingMessage
from .base import Connector

if TYPE_CHECKING:
    from ..config import Settings
    from ..service import ScheduleService

log = logging.getLogger(__name__)

MAX_IMAGES = 4
MAX_IMAGE_BYTES = 15 * 1024 * 1024


class ConflictButton(discord.ui.DynamicItem[discord.ui.Button], template=r"sched:(?P<action>confirm|reject):(?P<id>\d+)"):
    """重複確認ボタン。custom_id にイベントIDを持たせ、Bot 再起動後も押せるようにする。"""

    def __init__(self, action: str, event_id: int):
        label, style = ("追加する", discord.ButtonStyle.primary) if action == "confirm" else ("追加しない", discord.ButtonStyle.secondary)
        super().__init__(discord.ui.Button(label=label, style=style, custom_id=f"sched:{action}:{event_id}"))
        self.action = action
        self.event_id = event_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Item, match: re.Match[str], /):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        connector: DiscordConnector = interaction.client.connector  # type: ignore[attr-defined]
        service = connector.service
        original = interaction.message.content if interaction.message else ""
        event = service.db.get_event(self.event_id)

        if event is None:
            await interaction.response.edit_message(content=f"{original}\n-# この予定は削除されています", view=None)
            return
        if event.status != "pending":
            state = "追加済み" if event.status == "confirmed" else "追加しないを選択済み"
            await interaction.response.edit_message(content=f"{original}\n-# この予定は既に処理されています（{state}）", view=None)
            return

        if self.action == "confirm":
            await interaction.response.edit_message(content=f"{original}\n→ **追加しました**", view=None)
            await service.confirm(self.event_id)
        else:
            await interaction.response.edit_message(content=f"{original}\n→ 追加しませんでした", view=None)
            await service.reject(self.event_id)


class _Client(discord.Client):
    def __init__(self, connector: "DiscordConnector", **kwargs):
        super().__init__(**kwargs)
        self.connector = connector

    async def setup_hook(self) -> None:
        self.add_dynamic_items(ConflictButton)

    async def on_ready(self) -> None:
        self.connector.state = "connected"
        log.info("Discord に接続しました: %s", self.user)

    async def on_disconnect(self) -> None:
        if self.connector.state == "connected":
            self.connector.state = "reconnecting"

    async def on_resumed(self) -> None:
        self.connector.state = "connected"

    async def on_message(self, message: discord.Message) -> None:
        await self.connector.on_message(message)


class DiscordConnector(Connector):
    name = "discord"

    def __init__(self, settings: "Settings", service: "ScheduleService"):
        self.settings = settings
        self.service = service
        intents = discord.Intents.default()
        intents.message_content = True  # Developer Portal で「Message Content Intent」を ON にする必要あり
        self.client = _Client(self, intents=intents)
        self.state = "stopped"
        self.error = ""
        self._task: Optional[asyncio.Task] = None

    # ---------- lifecycle ----------
    async def start(self) -> None:
        self.state = "connecting"
        self._task = asyncio.create_task(self._run(), name="discord-bot")

    async def _run(self) -> None:
        try:
            await self.client.start(self.settings.discord_token)
        except discord.LoginFailure:
            self.state, self.error = "error", "Bot トークンが無効です"
            log.error("Discord: %s", self.error)
        except discord.PrivilegedIntentsRequired:
            self.state = "error"
            self.error = "Developer Portal で Message Content Intent を有効にしてください"
            log.error("Discord: %s", self.error)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.state, self.error = "error", str(e)
            log.exception("Discord Bot が停止しました")

    async def stop(self) -> None:
        if not self.client.is_closed():
            await self.client.close()
        if self._task:
            self._task.cancel()
        self.state = "stopped"

    def status(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "error": self.error,
            "user": str(self.client.user) if self.client.user else None,
            "owner_configured": bool(self.settings.discord_owner_id),
        }

    # ---------- 受信 ----------
    def _should_handle(self, message: discord.Message) -> bool:
        if message.author.bot:
            return False
        if message.guild is None:  # DM はオーナーからのものだけ
            return not self.settings.discord_owner_id or message.author.id == self.settings.discord_owner_id
        if not self.settings.discord_read_channels:  # Windows 通知で読む場合など
            return False
        watch = self.settings.discord_watch_channel_ids
        if not watch:
            return True
        channel_ids = {message.channel.id, getattr(message.channel, "parent_id", None)}
        return bool(channel_ids & set(watch))

    async def _collect(self, message: discord.Message) -> tuple[str, list[ImageInput]]:
        texts = [message.content]
        attachments = list(message.attachments)
        # 転送されたメッセージ（Forward）の本文・画像も対象にする
        for snap in getattr(message, "message_snapshots", None) or []:
            texts.append(snap.content or "")
            attachments.extend(snap.attachments)
        images: list[ImageInput] = []
        for a in attachments:
            if len(images) >= MAX_IMAGES:
                break
            if (a.content_type or "").startswith("image/") and a.size <= MAX_IMAGE_BYTES:
                try:
                    images.append(ImageInput(data=await a.read(), mime=a.content_type or "image/png", filename=a.filename))
                except discord.HTTPException as e:
                    log.warning("画像の取得に失敗: %s", e)
        return "\n".join(t for t in texts if t).strip(), images

    async def on_message(self, message: discord.Message) -> None:
        if not self._should_handle(message):
            return
        text, images = await self._collect(message)
        if not text and not images:
            return
        is_dm = message.guild is None
        channel = "DM" if is_dm else f"{message.guild.name} #{getattr(message.channel, 'name', '')}"
        msg = IncomingMessage(
            source="discord",
            message_id=str(message.id),
            text=text,
            sent_at=message.created_at,
            author=message.author.display_name,
            channel=channel,
            url=message.jump_url,
            images=images,
        )
        try:
            if is_dm:
                async with message.channel.typing():
                    outcome = await self.service.handle_message(msg)
            else:
                outcome = await self.service.handle_message(msg)
        except LLMUnavailable as e:
            log.error("ローカルLLMに接続できません: %s", e)
            if is_dm:
                await message.reply(f"ローカルLLMに接続できないため精査できませんでした。\n-# {e}")
            return
        except Exception:
            log.exception("メッセージの精査に失敗しました")
            if is_dm:
                await message.reply("メッセージの精査中にエラーが発生しました。")
            return

        # DM で直接送られた／転送されたのに予定がなかった場合だけ返信（チャンネルでは静かにする）
        if is_dm and not outcome.results:
            await message.reply(outcome.skipped or "予定は見つかりませんでした")
        elif is_dm:
            dups = [r for r in outcome.results if r.status == "duplicate"]
            for r in dups:
                await message.reply(f"既に登録済みです: {r.event.to_dict()['label']}")

    # ---------- 通知 (NotificationSink) ----------
    async def _owner(self) -> Optional[discord.User]:
        owner_id = self.settings.discord_owner_id
        if not owner_id:
            log.warning("DISCORD_OWNER_ID が未設定のため DM 通知を送れません")
            return None
        if not self.client.is_ready():
            return None
        return self.client.get_user(owner_id) or await self.client.fetch_user(owner_id)

    @staticmethod
    def _details(event: Event) -> str:
        parts = [format_time_range(event)]
        if event.location:
            parts.append(event.location)
        if event.source_url:
            parts.append(f"[元メッセージ]({event.source_url})")
        return "-# " + " ・ ".join(parts)

    async def notify_added(self, event: Event, message: str) -> None:
        owner = await self._owner()
        if owner:
            await owner.send(f"予定を追加しました\n**{message}**\n{self._details(event)}")

    async def notify_conflict(self, event: Event, conflicts: list[Event], message: str) -> None:
        owner = await self._owner()
        if not owner:
            return
        view = discord.ui.View(timeout=None)
        view.add_item(ConflictButton("confirm", event.id))
        view.add_item(ConflictButton("reject", event.id))
        first, *rest = message.split("\n")
        body = f"**確認: {first}**\n" + "\n".join(rest) + f"\n{self._details(event)}"
        await owner.send(body, view=view)
