#!/usr/bin/env bash
# Mac・Linux で、ログインしたときにスケジュール管理アプリを自動で起動するように登録します（管理者権限は不要）。
#   macOS : launchd のユーザーエージェント（~/Library/LaunchAgents/local.schedule-app.plist）
#   Linux : systemd のユーザーサービス（~/.config/systemd/user/schedule-app.service）
#           systemd が使えない環境では ~/.config/autostart/schedule-app.desktop（デスクトップにログインしたとき）
#
# 使い方（schedule-app フォルダで）:
#   bash scripts/install_autostart.sh
# 解除:
#   bash scripts/uninstall_autostart.sh
# Windows では scripts\install_autostart.ps1 を使います。
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAME="schedule-app"
LABEL="local.schedule-app"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}"

# .venv があればその Python を使う
PYTHON="$APP_DIR/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    PYTHON="$(command -v python3 || true)"
fi
if [ -z "$PYTHON" ]; then
    echo "python3 が見つかりません。README のセットアップ（.venv の作成）を先に行ってください。" >&2
    exit 1
fi

# 必要なライブラリが入っているか先に確かめる（入っていないと、画面が出ないまま起動に失敗するため）
if ! "$PYTHON" -c "import importlib.util as u, sys; sys.exit(0 if all(u.find_spec(m) for m in ('uvicorn', 'fastapi', 'httpx')) else 1)"; then
    echo "必要なライブラリが入っていません（$PYTHON）。先に次を実行してください:" >&2
    echo "  python3 -m venv .venv" >&2
    echo "  .venv/bin/python -m pip install -r requirements.txt" >&2
    exit 1
fi
mkdir -p "$APP_DIR/data"
LOG="$APP_DIR/data/autostart.log"

# /bin/sh 用に '...' で囲む
sh_quote() {
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

# systemd の設定ファイル用に "..." で囲む（\ " $ % を書けるようにする）。
# 実行するプログラムそのもの（ExecStart の最初）では効かないので、/usr/bin/env の引数として使う
unit_quote() {
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//\$/\$\$}"
    s="${s//%/%%}"
    printf '"%s"' "$s"
}

install_macos() {
    local plist="$HOME/Library/LaunchAgents/$LABEL.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    "$PYTHON" - "$plist" "$PYTHON" "$APP_DIR" "$LABEL" "$LOG" <<'PY'
import plistlib
import sys

path, python, app_dir, label, log = sys.argv[1:6]
with open(path, "wb") as f:
    plistlib.dump(
        {
            "Label": label,
            "ProgramArguments": [python, "run.py"],
            "WorkingDirectory": app_dir,
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},  # 異常終了したときだけ起動し直す
            "ProcessType": "Interactive",  # 裏で動いていても処理を遅らせない
            "StandardOutPath": log,
            "StandardErrorPath": log,
        },
        f,
    )
PY
    # 登録し直すときは、前のものが完全に止まってから登録する（アプリの終了には数秒かかる）
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    for _ in $(seq 1 30); do
        launchctl print "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || break
        sleep 1
    done
    launchctl bootstrap "gui/$(id -u)" "$plist"
    echo "自動起動を登録し、アプリを起動しました（$plist）"
    echo "  止める:       launchctl bootout gui/$(id -u)/$LABEL"
    echo "  起動し直す:   launchctl kickstart -k gui/$(id -u)/$LABEL"
    local fda
    fda="$(cd "$APP_DIR" && "$PYTHON" -c 'from app.connectors.macos_notifications import running_python; print(running_python())' 2>/dev/null || true)"
    echo ""
    echo "通知を読むには「フルディスクアクセス」の許可が必要です（システム設定 → プライバシーとセキュリティ → フルディスクアクセス）。"
    echo "自動起動では、次の Python をオンにしてから「起動し直す」を実行してください:"
    echo "  ${fda:-$PYTHON}"
}

install_systemd() {
    local unit_dir="$CONFIG_DIR/systemd/user"
    local unit="$unit_dir/$NAME.service"
    mkdir -p "$unit_dir"
    cat > "$unit" <<EOF
[Unit]
Description=スケジュール管理アプリ（schedule-app）

[Service]
Type=simple
WorkingDirectory=${APP_DIR//%/%%}
ExecStart=/usr/bin/env $(unit_quote "$PYTHON") run.py
Restart=on-failure
RestartSec=60

[Install]
WantedBy=default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable "$NAME.service"
    systemctl --user restart "$NAME.service"
    echo "自動起動を登録し、アプリを起動しました（$unit）"
    echo "  止める:       systemctl --user stop $NAME"
    echo "  起動し直す:   systemctl --user restart $NAME"
    echo "  状態とログ:   systemctl --user status $NAME"
}

install_xdg() {
    local launcher_dir="$CONFIG_DIR/$NAME"
    local launcher="$launcher_dir/start.sh"
    local desktop="$CONFIG_DIR/autostart/$NAME.desktop"
    mkdir -p "$launcher_dir" "$CONFIG_DIR/autostart"
    {
        echo "#!/bin/sh"
        echo "# スケジュール管理アプリの起動（scripts/install_autostart.sh が作成）"
        echo "cd $(sh_quote "$APP_DIR") || exit 1"
        echo "exec $(sh_quote "$PYTHON") run.py >> $(sh_quote "$LOG") 2>&1"
    } > "$launcher"
    chmod +x "$launcher"
    cat > "$desktop" <<EOF
[Desktop Entry]
Type=Application
Name=スケジュール管理アプリ
Comment=Discord などの連絡から予定を自動で登録する
Exec=/bin/sh "${launcher//\"/\\\"}"
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
EOF
    nohup /bin/sh "$launcher" >/dev/null 2>&1 &
    echo "自動起動を登録し、アプリを起動しました（$desktop）"
    echo "（systemd のユーザーサービスが使えないため、デスクトップにログインしたときに起動します）"
}

case "$(uname -s)" in
    Darwin) install_macos ;;
    Linux)
        if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
            install_systemd
        else
            install_xdg
        fi
        ;;
    *)
        echo "この OS には対応していません（Windows では scripts\\install_autostart.ps1 を使います）" >&2
        exit 1
        ;;
esac
PORT="$(cd "$APP_DIR" && "$PYTHON" -c 'from app.config import settings; print(settings.port)' 2>/dev/null || echo 8000)"
echo ""
echo "Web UI: http://127.0.0.1:$PORT  /  ログ: $APP_DIR/data/app.log"
echo "（手動で起動したアプリが動いている間は、自動起動のほうはすぐに終了します。手動のほうを止めてから起動し直してください）"
