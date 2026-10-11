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
from .llm import EmbeddedOllama, OllamaClient
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
    runtime: Optional[EmbeddedOllama] = None  # 同梱の Ollama（LLM_RUNTIME=external なら None）


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

        async def select_model() -> None:
            sel = await client.refresh_selection()
            if sel.available:
                log.info("ローカルLLM: テキスト=%s / 画像=%s", sel.text_model, sel.vision_model or "なし")
                if ctx.queue_task:
                    ctx.queue_task.kick()  # 起動を待っていたメッセージをすぐ精査する
            else:
                log.warning(sel.error)

        ctx.queue_task = PeriodicTask("queue-retry", service.process_queue, settings.queue_retry_sec)
        if llm is None and settings.llm_runtime_mode == "embedded":
            # 同梱の Ollama は準備（初回はダウンロード）に時間がかかるので、裏で進めて、できたらモデルを選ぶ
            ctx.runtime = EmbeddedOllama(settings)
            ctx.runtime.on_ready.append(select_model)
            ctx.runtime.start()
        else:
            await select_model()
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
            if ctx.runtime:
                await ctx.runtime.stop()
            if ctx.gcal:
                await ctx.gcal.aclose()
            await client.aclose()
            db.close()

    app = FastAPI(title="スケジュール管理", lifespan=lifespan)
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def revalidate_ui(request: Request, call_next):
        # アプリを更新したあとに、ブラウザが古い画面（app.js など）を使い続けないよう、毎回確認させる
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/", include_in_schema=False)
    async def index(request: Request, code: Optional[str] = None, state: Optional[str] = None, error: Optional[str] = None):
        # Google の連携画面から戻ってきたとき（リダイレクト先を http://127.0.0.1:<port>/ にしている）
        gcal = request.app.state.ctx.gcal
        if state and (code or error) and gcal and gcal.has_pending(state):
            return await google_callback_page(gcal, code, state, error)
        return FileResponse(STATIC_DIR / "index.html")

    return app
