"""連絡ツール接続の共通インターフェース。

新しいツール（Slack, Zoom など）を追加する手順:
  1. このファイルの Connector を継承したクラスを app/connectors/<tool>.py に作る
  2. 受信したメッセージを IncomingMessage に変換して service.handle_message() に渡す
  3. 通知もそのツールに送りたい場合は notify_added / notify_conflict を実装し、
     notifier.register(connector) で登録する（NotificationSink）
  4. app/connectors/__init__.py の build_connectors() に追加する
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class Connector(ABC):
    name: str = "base"

    @abstractmethod
    async def start(self) -> None:
        """受信を開始する（ブロックしないこと。必要ならタスクを作る）。"""

    @abstractmethod
    async def stop(self) -> None:
        ...

    def status(self) -> dict:
        return {"name": self.name, "state": "unknown"}
