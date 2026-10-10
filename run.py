"""起動スクリプト:  python run.py

自動起動（scripts/install_autostart.ps1）では pythonw.exe（コンソールなし）で起動するため、
ログは data/app.log にも書き出す。
"""
import logging
import os
import socket
import sys
from logging.handlers import RotatingFileHandler

# pythonw.exe では標準出力がないため、print などで落ちないように捨て先を用意する
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

import uvicorn  # noqa: E402

from app.config import settings  # noqa: E402
from app.main import create_app  # noqa: E402


def setup_logging() -> None:
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(settings.log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s", datefmt="%H:%M:%S"))
    logging.basicConfig(level=logging.INFO, handlers=[console, file_handler])
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def already_running() -> bool:
    """同じポートでアプリが既に動いていれば True（自動起動と手動起動の二重起動を防ぐ）。"""
    host = "127.0.0.1" if settings.host in ("0.0.0.0", "::", "") else settings.host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, settings.port)) == 0


if __name__ == "__main__":
    setup_logging()
    if already_running():
        logging.getLogger("run").warning(
            "既に起動しています（http://%s:%s）。二重起動を避けるため終了します", settings.host, settings.port
        )
        sys.exit(0)
    uvicorn.run(create_app(), host=settings.host, port=settings.port, log_level="warning", log_config=None)
