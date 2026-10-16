"""Web UI 用の REST API。"""
from __future__ import annotations

import html
import uuid
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

from ..audience import ACTIONS, AUDIENCE_LABELS
from ..gcal import GoogleAuthError
from ..holidays import holidays_between
from ..llm import LLMUnavailable
from ..models import Event, ImageInput, IncomingMessage
from ..service import _parse_time

router = APIRouter(prefix="/api")

MAX_PASTE_IMAGES = 4
MAX_PASTE_IMAGE_BYTES = 20 * 1024 * 1024


def _ctx(request: Request):
    return request.app.state.ctx


class EventIn(BaseModel):
    title: str
    date: date
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    location: str = ""
    description: str = ""


@router.get("/status")
async def status(request: Request):
    ctx = _ctx(request)
    return {
        "llm": _llm_status(ctx),
        "connectors": [c.status() for c in ctx.connectors],
        "pending_count": ctx.service.pending_count(),
        "unread_count": ctx.db.unread_count(),
        "today": ctx.service.today().isoformat(),
        "queue": ctx.db.queue_counts(),
        "google": ctx.gcal.status() if ctx.gcal else {"state": "disabled"},
        "power": ctx.sleep_guard.status() if ctx.sleep_guard else {"supported": False},
    }


def _llm_status(ctx) -> dict:
    runtime = ctx.runtime.status() if ctx.runtime else {"mode": "external"}
    return {**ctx.llm.status(), "runtime": runtime}


@router.post("/llm/refresh")
async def refresh_llm(request: Request):
    ctx = _ctx(request)
    if ctx.runtime and ctx.runtime.state in ("error", "stopped"):
        ctx.runtime.start()  # 同梱の Ollama の準備に失敗していたら、やり直す（できたらモデルも選び直す）
        return _llm_status(ctx)
    sel = await ctx.llm.refresh_selection()
    if sel.available and ctx.queue_task:
        ctx.queue_task.kick()  # つながったら、精査できなかったメッセージをすぐ精査する
    return _llm_status(ctx)


@router.post("/queue/retry")
async def retry_queue(request: Request):
    """精査をあきらめたメッセージを、もう一度精査する。"""
    ctx = _ctx(request)
    n = ctx.db.retry_failed()
    if ctx.queue_task:
        ctx.queue_task.kick()
    return {"retried": n}


@router.get("/events")
async def list_events(request: Request, date_from: Optional[date] = None, date_to: Optional[date] = None, include_past: bool = False):
    ctx = _ctx(request)
    if date_from is None and not include_past:
        date_from = ctx.service.today()
    events = ctx.db.list_events(statuses=("confirmed",), date_from=date_from, date_to=date_to)
    return [e.to_dict() for e in events]


@router.get("/holidays")
async def list_holidays(date_from: date, date_to: date):
    """カレンダーに表示する祝日・休日（date_from〜date_to、両端を含む）。"""
    if date_to < date_from:
        raise HTTPException(400, "date_to は date_from 以降にしてください")
    if (date_to - date_from).days > 400:
        raise HTTPException(400, "期間は 400 日以内にしてください")
    return [{"date": d.isoformat(), "name": name} for d, name in holidays_between(date_from, date_to)]


@router.post("/events")
async def create_event(request: Request, body: EventIn):
    ctx = _ctx(request)
    title = body.title.strip()
    if not title:
        raise HTTPException(400, "タイトルを入力してください")
    start = _parse_time(body.start_time or "")
    end = _parse_time(body.end_time or "") if start else None
    if end and end <= start:
        end = None
    event = Event(
        title=title,
        date=body.date,
        start_time=start,
        end_time=end,
        location=body.location.strip(),
        description=body.description.strip(),
        source="manual",
        source_message_id=f"manual-{uuid.uuid4().hex[:8]}",
    )
    result = await ctx.service.add_event(event)
    return result.to_dict()


@router.delete("/events/{event_id}")
async def delete_event(request: Request, event_id: int):
    ctx = _ctx(request)
    if not ctx.db.delete_event(event_id):
        raise HTTPException(404, "予定が見つかりません")
    if ctx.gcal_task:
        ctx.gcal_task.kick()  # Google カレンダーからも消す
    return {"ok": True}


@router.get("/pending")
async def pending(request: Request):
    return _ctx(request).service.pending_with_conflicts()


@router.post("/events/{event_id}/confirm")
async def confirm(request: Request, event_id: int):
    try:
        return (await _ctx(request).service.confirm(event_id)).to_dict()
    except KeyError:
        raise HTTPException(404, "予定が見つかりません")


@router.post("/events/{event_id}/reject")
async def reject(request: Request, event_id: int):
    try:
        return (await _ctx(request).service.reject(event_id)).to_dict()
    except KeyError:
        raise HTTPException(404, "予定が見つかりません")


@router.post("/events/{event_id}/adopt")
async def adopt(request: Request, event_id: int):
    """他の人の予定として見送った予定を、自分の予定として登録する。"""
    try:
        return (await _ctx(request).service.adopt(event_id)).to_dict()
    except KeyError:
        raise HTTPException(404, "予定が見つかりません")
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/events/{event_id}/merge")
async def merge(request: Request, event_id: int, into: Optional[int] = None):
    """確認待ちの予定を「同じ予定」として既存の予定にまとめる（into 省略時は一番近い候補）。"""
    try:
        return (await _ctx(request).service.merge_pending(event_id, into)).to_dict()
    except KeyError:
        raise HTTPException(404, "予定が見つかりません")
    except ValueError as e:
        raise HTTPException(409, str(e))


@router.post("/changes/{change_id}/apply")
async def apply_change(request: Request, change_id: int):
    """確認待ちの変更・取り消しを反映する。"""
    try:
        return (await _ctx(request).service.apply_change(change_id)).to_dict()
    except KeyError:
        raise HTTPException(404, "変更が見つかりません")


@router.post("/changes/{change_id}/reject")
async def reject_change(request: Request, change_id: int):
    try:
        return (await _ctx(request).service.reject_change(change_id)).to_dict()
    except KeyError:
        raise HTTPException(404, "変更が見つかりません")


@router.post("/changes/{change_id}/undo")
async def undo_change(request: Request, change_id: int):
    """反映した変更・取り消しを元に戻す。"""
    try:
        return (await _ctx(request).service.undo_change(change_id)).to_dict()
    except KeyError:
        raise HTTPException(404, "予定が見つかりません")
    except ValueError as e:
        raise HTTPException(409, str(e))


class AudienceRulesIn(BaseModel):
    names: Optional[list[str]] = None
    groups: Optional[list[str]] = None
    actions: Optional[dict[str, str]] = None


def _rules_response(rules) -> dict:
    return {**rules.to_dict(), "labels": AUDIENCE_LABELS}


@router.get("/settings/audience")
async def get_audience_rules(request: Request):
    """登録の基準（自分の名前・所属グループ・宛先ごとの扱い）。"""
    return _rules_response(_ctx(request).service.audience_rules())


@router.put("/settings/audience")
async def put_audience_rules(request: Request, body: AudienceRulesIn):
    data = body.model_dump(exclude_none=True)
    for key, value in (data.get("actions") or {}).items():
        if key not in AUDIENCE_LABELS or value not in ACTIONS:
            raise HTTPException(400, f"扱いは {' / '.join(ACTIONS)} のどれかにしてください: {key}={value}")
    return _rules_response(_ctx(request).service.save_audience_rules(data))


@router.get("/notifications")
async def notifications(request: Request, limit: int = 100):
    ctx = _ctx(request)
    items = []
    for n in ctx.db.list_notifications(limit):
        d = n.to_dict()
        d["undoable"] = bool(
            n.change_id and n.kind in ("updated", "cancelled", "merged") and ctx.service.is_undoable(n.change_id)
        )
        ev = ctx.db.get_event(n.event_id) if n.kind == "skipped" and n.event_id else None
        d["adoptable"] = bool(ev and ev.status == "skipped")  # 見送った予定を「追加する」で登録できる
        items.append(d)
    return items


@router.post("/notifications/read")
async def mark_read(request: Request):
    _ctx(request).db.mark_all_read()
    return {"ok": True}


@router.post("/analyze")
async def analyze(request: Request, text: str = Form(""), images: list[UploadFile] = File(default=[])):
    """貼り付けたメッセージ・画像を精査し、終わるまで待って結果を返す（curl などでの動作確認用）。

    Web UI は、待たずに裏で精査する POST /api/analyze/jobs を使う。
    """
    ctx = _ctx(request)
    imgs = []
    for f in images[:4]:
        data = await f.read()
        if data and (f.content_type or "").startswith("image/"):
            imgs.append(ImageInput(data=data, mime=f.content_type, filename=f.filename or ""))
    if not text.strip() and not imgs:
        raise HTTPException(400, "メッセージまたは画像を入力してください")
    msg = IncomingMessage(
        source="manual",
        message_id=f"paste-{uuid.uuid4().hex[:8]}",
        text=text,
        sent_at=datetime.now(ctx.settings.tz),
        author="",
        channel="貼り付け",
        images=imgs,
    )
    try:
        outcome = await ctx.service.handle_message(msg, force=True)
    except LLMUnavailable as e:
        raise HTTPException(503, f"ローカルLLMに接続できません: {e}")
    except Exception as e:
        raise HTTPException(500, f"精査に失敗しました: {e}")
    return outcome.to_dict()


@router.post("/analyze/jobs", status_code=202)
async def submit_analyze_job(request: Request, text: str = Form(""), images: list[UploadFile] = File(default=[])):
    """貼り付けたメッセージ・画像を受け付け、裏で精査する（結果は GET /api/analyze/jobs/{id} と通知タブ）。

    画像は CPU だけの PC だと数分かかるので、/api/analyze のように終わるまで待たせない。
    ローカルLLM の準備中でも受け付け、つながったら精査する。
    """
    ctx = _ctx(request)
    if len(images) > MAX_PASTE_IMAGES:
        raise HTTPException(400, f"画像は {MAX_PASTE_IMAGES} 枚までです")
    imgs = []
    for f in images:
        if not (f.content_type or "").startswith("image/"):
            raise HTTPException(400, f"画像ではないファイルです: {f.filename or '（名前なし）'}")
        data = await f.read(MAX_PASTE_IMAGE_BYTES + 1)
        if len(data) > MAX_PASTE_IMAGE_BYTES:
            raise HTTPException(413, f"画像が大きすぎます（{MAX_PASTE_IMAGE_BYTES // 1024 // 1024}MB まで）: {f.filename or '（名前なし）'}")
        if data:
            imgs.append(ImageInput(data=data, mime=f.content_type, filename=f.filename or ""))
    if not text.strip() and not imgs:
        raise HTTPException(400, "メッセージまたは画像を入力してください")
    job_id = ctx.service.submit_paste(text, imgs)
    if ctx.queue_task:
        ctx.queue_task.kick()
    return ctx.service.paste_status(job_id)


@router.get("/analyze/jobs")
async def list_analyze_jobs(request: Request):
    """まだ終わっていない貼り付けの一覧（画面を開き直したとき用）。"""
    return _ctx(request).service.paste_jobs()


@router.get("/analyze/jobs/{job_id}")
async def analyze_job(request: Request, job_id: str):
    return _ctx(request).service.paste_status(job_id)


# ---------- Google カレンダー ----------
def _page(title: str, message: str, ok: bool) -> HTMLResponse:
    color = "#34c759" if ok else "#ff3b30"
    body = f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,"Hiragino Sans","Yu Gothic UI",sans-serif;background:#f2f2f7;
display:grid;place-items:center;min-height:100vh;margin:0}}main{{background:#fff;border-radius:14px;padding:32px 36px;
max-width:460px;text-align:center}}h1{{font-size:20px;color:{color}}}p{{color:#3c3c43;line-height:1.6}}a{{color:#007aff}}</style>
</head><body><main><h1>{html.escape(title)}</h1><p>{html.escape(message)}</p><p><a href="/">スケジュールに戻る</a></p></main></body></html>"""
    return HTMLResponse(body, status_code=200 if ok else 400)


async def google_callback_page(gcal, code: Optional[str], state: str, error: Optional[str]) -> HTMLResponse:
    try:
        await gcal.complete_auth(code, state, error)
    except GoogleAuthError as e:
        return _page("連携できませんでした", str(e), ok=False)
    return _page(
        "Google カレンダーと連携しました",
        f"確定した予定を「{gcal.settings.google_calendar_name}」カレンダーに登録します。このタブは閉じてかまいません。",
        ok=True,
    )


def _gcal(request: Request):
    gcal = _ctx(request).gcal
    if gcal is None:
        raise HTTPException(400, "Google カレンダー連携が無効です（.env の GOOGLE_CALENDAR と credentials.json を確認してください）")
    return gcal


@router.get("/google/login")
async def google_login(request: Request):
    """Google の許可画面へ移動する。許可すると http://127.0.0.1:<port>/ に戻ってくる。"""
    ctx = _ctx(request)
    if ctx.gcal is None or ctx.gcal.state in ("no_credentials", "bad_credentials"):
        return _page(
            "Google カレンダー連携の準備ができていません",
            f"Google Cloud で作成した OAuth クライアント（デスクトップ アプリ）の JSON を "
            f"{ctx.settings.google_credentials_path} に置いてから、アプリを再起動してください（README 参照）。",
            ok=False,
        )
    redirect_uri = f"http://127.0.0.1:{ctx.settings.port}/"
    return RedirectResponse(ctx.gcal.authorization_url(redirect_uri))


@router.post("/google/disconnect")
async def google_disconnect(request: Request):
    gcal = _gcal(request)
    await gcal.disconnect()
    return gcal.status()


@router.post("/google/sync")
async def google_sync(request: Request):
    gcal = _gcal(request)
    await gcal.sync_once()
    return gcal.status()
