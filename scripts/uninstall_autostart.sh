#!/usr/bin/env bash
# install_autostart.sh で登録した自動起動を解除し、自動起動で動いているアプリを止めます。
#   bash scripts/uninstall_autostart.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"  # 動いているアプリの作業フォルダ（本当の場所）と比べるため
NAME="schedule-app"
LABEL="local.schedule-app"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}"

case "$(uname -s)" in
    Darwin)
        launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
        rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
        ;;
    Linux)
        if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
            systemctl --user disable --now "$NAME.service" 2>/dev/null || true
            rm -f "$CONFIG_DIR/systemd/user/$NAME.service"
            systemctl --user daemon-reload 2>/dev/null || true
        fi
        if [ -f "$CONFIG_DIR/$NAME/start.sh" ]; then
            # デスクトップの自動起動で動いているアプリ（作業フォルダがこのフォルダの run.py）を止める
            for pid in $(pgrep -f "run.py" 2>/dev/null || true); do
                if [ "$(readlink "/proc/$pid/cwd" 2>/dev/null || true)" = "$APP_DIR" ]; then
                    kill "$pid" 2>/dev/null || true
                fi
            done
        fi
        rm -f "$CONFIG_DIR/autostart/$NAME.desktop" "$CONFIG_DIR/$NAME/start.sh"
        rmdir "$CONFIG_DIR/$NAME" 2>/dev/null || true
        ;;
    *)
        echo "この OS には対応していません（Windows では scripts\\uninstall_autostart.ps1 を使います）" >&2
        exit 1
        ;;
esac
echo "自動起動を解除しました。"
