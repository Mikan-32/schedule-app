"""アプリの実行中に PC がスリープしないようにする（Windows・macOS・Linux）。

- Windows: SetThreadExecutionState（動画プレーヤーなどと同じ仕組み）
- macOS  : caffeinate -i -w <アプリの PID>（アプリが終わると caffeinate も終わる）
- Linux  : systemd-inhibit --what=sleep:idle（ログイン中のセッションのスリープ・何もしないときのスリープを止める。
           アプリが終わると 30 秒以内に解除される）
どれも電源設定そのものは変更せず、アプリが終了すると元に戻る。
ノートPC のカバーを閉じたときや、手動でスリープしたときは防げない。
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Callable, Optional

from .config import os_kind

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


def mac_on_ac_power() -> bool:
    """pmset -g batt の「Now drawing from 'AC Power'」/「'Battery Power'」で判断する。"""
    out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
    return "Battery Power" not in out


POWER_SUPPLY = Path("/sys/class/power_supply")
AC_TYPES = {"Mains", "USB", "USB_C", "USB_PD", "USB_PD_DRP", "USB_DCP", "USB_CDP", "USB_ACA", "Wireless"}


def linux_on_ac_power(root: Path = POWER_SUPPLY) -> bool:
    """電源につながっているか（/sys/class/power_supply）。バッテリーのない PC は常に True。"""
    try:
        devices = list(root.iterdir())
    except OSError:
        return True
    has_battery = False
    for dev in devices:
        try:
            kind = (dev / "type").read_text().strip()
            if kind == "Battery":
                scope = dev / "scope"
                if scope.exists() and scope.read_text().strip() == "Device":
                    continue  # ワイヤレスのマウス・キーボードなどの電池
                has_battery = True
            elif kind in AC_TYPES and (dev / "online").read_text().strip() == "1":
                return True
        except OSError:
            continue
    return not has_battery


class ProcessInhibitor:
    """スリープを防ぐコマンド（caffeinate / systemd-inhibit）を、スリープを防ぎたい間だけ動かしておく。"""

    START_CHECK_SEC = 0.3  # 起動直後に終わってしまったら（許可がない等）エラーにする

    def __init__(self, command: list[str]):
        self.command = command
        self._proc: Optional[subprocess.Popen] = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def __call__(self, keep_awake: bool) -> None:
        if keep_awake:
            if not self.running:
                self._start()
        else:
            self._stop()

    def _start(self) -> None:
        proc = subprocess.Popen(
            self.command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",  # 止めるときに、子プロセス（systemd-inhibit が動かす sh）ごと終わらせる
        )
        try:
            proc.wait(timeout=self.START_CHECK_SEC)
        except subprocess.TimeoutExpired:
            self._proc = proc
            return
        err = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace").strip()
        raise RuntimeError(err or f"{self.command[0]} が終了しました（終了コード {proc.returncode}）")

    def _stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.poll() is not None:
            return
        killpg = getattr(os, "killpg", None)  # Windows にはない
        try:
            if killpg is None:
                raise OSError("killpg がありません")
            killpg(proc.pid, signal.SIGTERM)
        except OSError:
            proc.terminate()
        try:
            proc.wait(3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(3)


def platform_backend() -> tuple[Optional[Callable[[bool], None]], Optional[Callable[[], bool]], str]:
    """この OS でスリープを防ぐ方法: (set_state, on_ac_power, 方式の名前)。使えなければ (None, None, "")。"""
    kind = os_kind()
    if kind == "windows":
        return _win_set_state, _win_on_ac_power, "windows"
    pid = str(os.getpid())
    if kind == "macos" and shutil.which("caffeinate"):
        return ProcessInhibitor(["caffeinate", "-i", "-w", pid]), mac_on_ac_power, "caffeinate"
    if kind == "linux" and shutil.which("systemd-inhibit"):
        # アプリ（pid）が終わったら sh も終わり、systemd-inhibit がスリープの防止を解除する
        watch = f"while kill -0 {pid} 2>/dev/null; do sleep 30; done"
        command = [
            "systemd-inhibit",
            "--what=sleep:idle",
            "--who=スケジュール管理アプリ",
            "--why=予定の連絡を受け取るため",
            "--mode=block",
            "sh",
            "-c",
            watch,
        ]
        return ProcessInhibitor(command), linux_on_ac_power, "systemd-inhibit"
    return None, None, ""


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
        method = "custom"
        if set_state is None:
            set_state, default_on_ac, method = platform_backend()
            on_ac_power = on_ac_power or default_on_ac
        self.supported = set_state is not None
        self.method = method if self.supported else ""
        self._set_state = set_state or (lambda keep: None)
        self._on_ac_power = on_ac_power or (lambda: True)
        self.active = False
        self.error = ""
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
            self._set_state(True)  # 念のため定期的に再設定する（止まっていたら動かし直す）

    def start(self) -> None:
        if not self.supported or self.mode == "off":
            return
        self._task = asyncio.create_task(self._run(), name="sleep-guard")

    async def _run(self) -> None:
        # SetThreadExecutionState はスレッドごとの設定なので、常にイベントループのスレッドから呼ぶ
        while True:
            try:
                self.apply()
                self.error = ""
            except Exception as e:
                if str(e) != self.error:  # 同じ失敗が続くときはログに 1 回だけ出す
                    log.warning("スリープ防止の設定に失敗しました: %s", e)
                self.error = str(e) or type(e).__name__
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
        return {"supported": self.supported, "mode": self.mode, "active": self.active, "method": self.method, "error": self.error}
