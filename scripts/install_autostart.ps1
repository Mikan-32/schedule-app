<#
  Windows にサインインしたとき、スケジュール管理アプリを自動で起動するように登録します。
  （タスク スケジューラに「ScheduleApp」というタスクを作ります。管理者権限は不要です）

  使い方（schedule-app フォルダで）:
    powershell -ExecutionPolicy Bypass -File scripts\install_autostart.ps1
  解除:
    powershell -ExecutionPolicy Bypass -File scripts\uninstall_autostart.ps1
#>
$ErrorActionPreference = "Stop"
$TaskName = "ScheduleApp"
$AppDir = Split-Path -Parent $PSScriptRoot

# コンソール画面を出さない pythonw.exe を使う（.venv があればそちらを優先）
$Pythonw = Join-Path $AppDir ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Pythonw)) {
    $cmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $cmd) {
        throw "pythonw.exe が見つかりません。README のセットアップ（.venv の作成）を先に行ってください。"
    }
    $Pythonw = $cmd.Source
}

# 必要なライブラリが入っているか先に確かめる（入っていないと、画面が出ないまま起動に失敗するため）
$Python = Join-Path (Split-Path -Parent $Pythonw) "python.exe"
& $Python -c "import importlib.util as u, sys; sys.exit(0 if all(u.find_spec(m) for m in ('uvicorn', 'fastapi', 'httpx')) else 1)"
if ($LASTEXITCODE -ne 0) {
    throw "必要なライブラリが入っていません（$Python）。先に次を実行してください:`n  python -m venv .venv`n  .venv\Scripts\python.exe -m pip install -r requirements.txt"
}

$User = "$env:USERDOMAIN\$env:USERNAME"
$Action = New-ScheduledTaskAction -Execute $Pythonw -Argument "run.py" -WorkingDirectory $AppDir
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

try {
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
        -Principal $Principal -Description "Discord などの連絡から予定を自動登録するアプリ（schedule-app）" -Force | Out-Null
    Write-Host "自動起動を登録しました（タスク名: $TaskName / 実行: $Pythonw）"
    Start-ScheduledTask -TaskName $TaskName
    Write-Host "止めるとき: Stop-ScheduledTask -TaskName $TaskName"
} catch {
    # 環境によってはタスク スケジューラに登録できないため、スタートアップ フォルダのショートカットで代用する
    Write-Warning "タスク スケジューラに登録できませんでした（$($_.Exception.Message)）。スタートアップ フォルダを使います"
    $Lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "$TaskName.lnk"
    $Shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($Lnk)
    $Shortcut.TargetPath = $Pythonw
    $Shortcut.Arguments = "run.py"
    $Shortcut.WorkingDirectory = $AppDir
    $Shortcut.Description = "スケジュール管理アプリ（schedule-app）"
    $Shortcut.Save()
    Write-Host "自動起動を登録しました（$Lnk）"
    Start-Process -FilePath $Pythonw -ArgumentList "run.py" -WorkingDirectory $AppDir
}
Write-Host "アプリを起動しました。Web UI: http://127.0.0.1:8000"
Write-Host "ログ: $AppDir\data\app.log"
