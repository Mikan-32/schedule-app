"""アプリ本体: Web サーバー + 各連絡ツールのコネクタ を 1 プロセスで動かす。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings, settings as default_settings
from .connectors import Connector, build_connectors
from .db import Database
from .llm import OllamaClient
from .notifier import Notifier
from .service import ScheduleService
from .web.api import router

log = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"


@dataclass
class AppContext:
    settings: Settings
    db: Database
    llm: OllamaClient
    notifier: Notifier
    service: ScheduleService
    connectors: list[Connector] = field(default_factory=list)


def create_app(settings: Settings | None = None, llm: OllamaClient | None = None, start_connectors: bool = True) -> FastAPI:
    settings = settings or default_settings

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(settings.db_path, tz=settings.tz)
        client = llm or OllamaClient(settings)
        notifier = Notifier(db)
        service = ScheduleService(settings, db, client, notifier)
        ctx = AppContext(settings, db, client, notifier, service)
        ctx.connectors = build_connectors(settings, service, notifier) if start_connectors else []
        app.state.ctx = ctx

        sel = await client.refresh_selection()
        if sel.available:
            log.info("ローカルLLM: テキスト=%s / 画像=%s", sel.text_model, sel.vision_model or "なし")
        else:
            log.warning(sel.error)
        for c in ctx.connectors:
            await c.start()
        log.info("Web UI: http://%s:%s", settings.host, settings.port)
        try:
            yield
        finally:
            for c in ctx.connectors:
                await c.stop()
            await client.aclose()
            db.close()

    app = FastAPI(title="スケジュール管理", lifespan=lifespan)
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC_DIR / "index.html")

    return app
