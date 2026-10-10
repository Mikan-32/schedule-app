"""一定間隔で（または kick() されたらすぐ）処理を繰り返すバックグラウンドタスク。"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)


class PeriodicTask:
    def __init__(self, name: str, func: Callable[[], Awaitable[object]], interval: float):
        self.name = name
        self.func = func
        self.interval = max(1.0, interval)
        self._event = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=self.name)

    def kick(self) -> None:
        """次の実行を待たずに、すぐ実行させる。"""
        self._event.set()

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            self._event.clear()
            try:
                await self.func()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s の実行に失敗しました", self.name)
            try:
                await asyncio.wait_for(self._event.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
