"""Web UI 用の REST API。"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel

from ..llm import LLMUnavailable
from ..models import Event, ImageInput, IncomingMessage
from ..service import _parse_time

router = APIRouter(prefix="/api")


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
        "llm": ctx.llm.status(),
        "connectors": [c.status() for c in ctx.connectors],
        "pending_count": ctx.service.pending_count(),
        "unread_count": ctx.db.unread_count(),
        "today": ctx.service.today().isoformat(),
    }


@router.post("/llm/refresh")
async def refresh_llm(request: Request):
    ctx = _ctx(request)
    await ctx.llm.refresh_selection()
    return ctx.llm.status()


@router.get("/events")
async def list_events(request: Request, date_from: Optional[date] = None, date_to: Optional[date] = None, include_past: bool = False):
    ctx = _ctx(request)
    if date_from is None and not include_past:
        date_from = ctx.service.today()
    events = ctx.db.list_events(statuses=("confirmed",), date_from=date_from, date_to=date_to)
    return [e.to_dict() for e in events]


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
    if not _ctx(request).db.delete_event(event_id):
        raise HTTPException(404, "予定が見つかりません")
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


@router.get("/notifications")
async def notifications(request: Request, limit: int = 100):
    ctx = _ctx(request)
    items = []
    for n in ctx.db.list_notifications(limit):
        d = n.to_dict()
        d["undoable"] = bool(n.change_id and n.kind in ("updated", "cancelled") and ctx.service.is_undoable(n.change_id))
        items.append(d)
    return items


@router.post("/notifications/read")
async def mark_read(request: Request):
    _ctx(request).db.mark_all_read()
    return {"ok": True}


@router.post("/analyze")
async def analyze(request: Request, text: str = Form(""), images: list[UploadFile] = File(default=[])):
    """貼り付けたメッセージ・画像を精査して予定登録する（Discord を使わない動作確認にも使える）。"""
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
