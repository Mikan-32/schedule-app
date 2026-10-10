<#
  install_autostart.ps1 で登録した自動起動を解除し、動いているアプリを止めます。
    powershell -ExecutionPolicy Bypass -File scripts\uninstall_autostart.ps1
#>
$ErrorActionPreference = "Stop"
$TaskName = "ScheduleApp"

$Removed = $false
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "自動起動を解除しました（タスク名: $TaskName）"
    $Removed = $true
}
$Lnk = Join-Path ([Environment]::GetFolderPath("Startup")) "$TaskName.lnk"
if (Test-Path $Lnk) {
    Remove-Item $Lnk
    Write-Host "スタートアップ フォルダのショートカットを削除しました。動いているアプリはタスク マネージャーで pythonw.exe を終了してください"
    $Removed = $true
}
if (-not $Removed) {
    Write-Host "自動起動は登録されていません"
}
