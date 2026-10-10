"""アプリ本体: Web サーバー + 各連絡ツールのコネクタ を 1 プロセスで動かす。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .background import PeriodicTask
from .config import Settings, settings as default_settings
from .connectors import Connector, build_connectors
from .db import Database
from .gcal import GoogleCalendarSync
from .llm import OllamaClient
from .notifier import Notifier
from .power import SleepGuard
from .service import ScheduleService
from .web.api import google_callback_page, router

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
    queue_task: Optional[PeriodicTask] = None  # 精査できなかったメッセージの再精査
    gcal: Optional[GoogleCalendarSync] = None
    gcal_task: Optional[PeriodicTask] = None
    sleep_guard: Optional[SleepGuard] = None


def create_app(
    settings: Settings | None = None,
    llm: OllamaClient | None = None,
    start_connectors: bool = True,
    gcal_transport: Optional[httpx.AsyncBaseTransport] = None,
) -> FastAPI:
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
        ctx.queue_task = PeriodicTask("queue-retry", service.process_queue, settings.queue_retry_sec)
        ctx.queue_task.start()
        if settings.google_calendar_enabled:
            ctx.gcal = GoogleCalendarSync(settings, db, transport=gcal_transport)
            ctx.gcal_task = PeriodicTask("google-calendar", ctx.gcal.sync_once, settings.google_sync_sec)
            ctx.gcal.on_connected = ctx.gcal_task.kick
            notifier.listeners.append(ctx.gcal_task.kick)  # 予定が変わったらすぐ同期する
            ctx.gcal_task.start()
        ctx.sleep_guard = SleepGuard(settings.prevent_sleep)
        ctx.sleep_guard.start()
        for c in ctx.connectors:
            await c.start()
        log.info("Web UI: http://%s:%s", settings.host, settings.port)
        try:
            yield
        finally:
            for c in ctx.connectors:
                await c.stop()
            for task in (ctx.queue_task, ctx.gcal_task):
                if task:
                    await task.stop()
            await ctx.sleep_guard.stop()
            if ctx.gcal:
                await ctx.gcal.aclose()
            await client.aclose()
            db.close()

    app = FastAPI(title="スケジュール管理", lifespan=lifespan)
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
        # Google の連携画面から戻ってきたとき（リダイレクト先を http://127.0.0.1:<port>/ にしている）
        gcal = request.app.state.ctx.gcal
        if state and (code or error) and gcal and gcal.has_pending(state):
            return await google_callback_page(gcal, code, state, error)
        return FileResponse(STATIC_DIR / "index.html")

    return app
