"""スリープ防止（Windows・macOS・Linux）のテスト。本物の電源設定には触れない。"""
import asyncio
import os
import sys
import types

import pytest

from app import power
from app.power import ProcessInhibitor, SleepGuard, linux_on_ac_power


def test_process_inhibitor_runs_only_while_needed():
    inhibitor = ProcessInhibitor([sys.executable, "-c", "import time; time.sleep(60)"])
    inhibitor(True)
    assert inhibitor.running
    pid = inhibitor._proc.pid
    inhibitor(True)  # 動いていれば何もしない
    assert inhibitor._proc.pid == pid
    inhibitor(False)
    assert not inhibitor.running


def test_process_inhibitor_reports_failure():
    # systemd-inhibit が許可されない・セッションバスがない等で、すぐに終わってしまう場合
    inhibitor = ProcessInhibitor([sys.executable, "-c", "import sys; sys.stderr.write('Failed to connect to bus'); sys.exit(1)"])
    with pytest.raises(RuntimeError, match="Failed to connect to bus"):
        inhibitor(True)
    assert not inhibitor.running


def test_linux_on_ac_power(tmp_path):
    def device(name, **files):
        d = tmp_path / name
        d.mkdir()
        for key, value in files.items():
            (d / key).write_text(value + "\n")

    assert linux_on_ac_power(tmp_path) is True  # 電源の情報がない（デスクトップ PC など）
    device("hidpp_battery_0", type="Battery", scope="Device")  # ワイヤレスのマウスの電池は数えない
    assert linux_on_ac_power(tmp_path) is True
    device("BAT0", type="Battery")
    device("AC", type="Mains", online="0")
    assert linux_on_ac_power(tmp_path) is False
    (tmp_path / "AC" / "online").write_text("1\n")
    assert linux_on_ac_power(tmp_path) is True
    assert linux_on_ac_power(tmp_path / "none") is True


def test_mac_on_ac_power(monkeypatch):
    out = {"text": "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1234)\t100%; charged; 0:00 remaining"}
    monkeypatch.setattr(power.subprocess, "run", lambda *a, **k: types.SimpleNamespace(stdout=out["text"]))
    assert power.mac_on_ac_power()
    out["text"] = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1234)\t80%; discharging"
    assert not power.mac_on_ac_power()


def test_platform_backend(monkeypatch):
    monkeypatch.setattr(power.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sys, "platform", "darwin")
    set_state, on_ac, method = power.platform_backend()
    assert method == "caffeinate" and on_ac is power.mac_on_ac_power
    assert set_state.command == ["caffeinate", "-i", "-w", str(os.getpid())]  # アプリが終わると caffeinate も終わる
    monkeypatch.setattr(sys, "platform", "linux")
    set_state, on_ac, method = power.platform_backend()
    assert method == "systemd-inhibit" and on_ac is power.linux_on_ac_power
    assert set_state.command[:3] == ["systemd-inhibit", "--what=sleep:idle", "--who=スケジュール管理アプリ"]
    assert f"kill -0 {os.getpid()}" in set_state.command[-1]  # アプリが終わると解除される
    monkeypatch.setattr(power.shutil, "which", lambda name: None)  # コマンドがない
    assert power.platform_backend() == (None, None, "")
    assert SleepGuard("ac").status()["supported"] is False
    monkeypatch.setattr(sys, "platform", "freebsd14")
    assert power.platform_backend() == (None, None, "")


async def test_sleep_guard_reports_error_once(caplog):
    def fail(keep):
        raise RuntimeError("Failed to connect to bus")

    guard = SleepGuard("always", set_state=fail)
    guard.CHECK_SEC = 0.01
    guard.start()
    await asyncio.sleep(0.1)
    await guard.stop()
    status = guard.status()
    assert status["error"] == "Failed to connect to bus" and not status["active"]
    assert sum("スリープ防止の設定に失敗" in r.getMessage() for r in caplog.records) == 1  # 何度失敗してもログは 1 回
