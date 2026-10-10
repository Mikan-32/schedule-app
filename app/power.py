"""アプリの実行中に PC がスリープしないようにする（Windows のみ）。

Windows の SetThreadExecutionState を使う。動画プレーヤーなどと同じ仕組みで、
アプリが終了すると自動的に元に戻る（電源設定そのものは変更しない）。
ノートPC のカバーを閉じたときや、手動でスリープしたときは防げない。
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import sys
from typing import Callable, Optional

log = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class _SystemPowerStatus(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


def _win_set_state(keep_awake: bool) -> None:
    flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if keep_awake else 0)
    ctypes.windll.kernel32.SetThreadExecutionState(flags)  # type: ignore[attr-defined]


def _win_on_ac_power() -> bool:
    status = _SystemPowerStatus()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(status)):  # type: ignore[attr-defined]
        return True
    return status.ACLineStatus != 0  # 0=バッテリー, 1=電源接続, 255=不明（デスクトップ等）


class SleepGuard:
    """mode: off / ac（電源接続中だけスリープを防ぐ）/ always"""

    CHECK_SEC = 60

    def __init__(
        self,
        mode: str,
        set_state: Optional[Callable[[bool], None]] = None,
        on_ac_power: Optional[Callable[[], bool]] = None,
    ):
        self.mode = mode if mode in ("off", "ac", "always") else "ac"
        self.supported = set_state is not None or sys.platform == "win32"
        self._set_state = set_state or _win_set_state
        self._on_ac_power = on_ac_power or _win_on_ac_power
        self.active = False
        self._task: Optional[asyncio.Task] = None

    def should_keep_awake(self) -> bool:
        if self.mode == "always":
            return True
        if self.mode == "ac":
            try:
                return self._on_ac_power()
            except Exception:
                return False
        return False

    def apply(self) -> None:
        keep = self.should_keep_awake()
        if keep != self.active:
            self._set_state(keep)
            self.active = keep
            log.info("スリープ防止: %s", "有効" if keep else "無効")
        elif keep:
            self._set_state(True)  # 念のため定期的に再設定する

    def start(self) -> None:
        if not self.supported or self.mode == "off":
            return
        self._task = asyncio.create_task(self._run(), name="sleep-guard")

    async def _run(self) -> None:
        # SetThreadExecutionState はスレッドごとの設定なので、常にイベントループのスレッドから呼ぶ
        while True:
            try:
                self.apply()
            except Exception as e:
                log.warning("スリープ防止の設定に失敗しました: %s", e)
            await asyncio.sleep(self.CHECK_SEC)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        if self.active:
            try:
                self._set_state(False)
            except Exception:
                pass
            self.active = False

    def status(self) -> dict:
        return {"supported": self.supported, "mode": self.mode, "active": self.active}
