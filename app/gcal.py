"""Google カレンダー同期（アプリ → Google カレンダーへの一方向）。

- 認証: OAuth 2.0（デスクトップ アプリ）+ PKCE。スコープは calendar.app.created
  （このアプリが作ったカレンダーだけを操作でき、ほかのカレンダーの中身は見えない）
- アプリ専用のカレンダーを1つ作り、「確定」した予定をそこへ追加・更新・削除する
- PC が消えていても、登録済みの予定はスマホの Google カレンダーで見られ、通知も届く
- Google カレンダー側で編集しても、アプリには反映されない（アプリ側で予定が変わると上書きされる）
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import secrets
import time as _time
from datetime import datetime, timedelta
from typing import Callable, Optional
from urllib.parse import quote, urlencode

import httpx

from .config import Settings
from .db import Database
from .models import Event

log = logging.getLogger(__name__)

SCOPE = "https://www.googleapis.com/auth/calendar.app.created"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://www.googleapis.com/calendar/v3"
PAST_DAYS = 30  # これより前の予定は同期しない
STATE_CALENDAR_ID = "gcal_calendar_id"
SOURCE_LABELS = {"discord": "Discord", "winnotify": "通知", "manual": "手動入力"}


class GoogleAuthError(RuntimeError):
    """連携していない・連携が切れた（再連携が必要）。"""


class GoogleApiError(RuntimeError):
    pass


def _error_text(r: httpx.Response) -> str:
    try:
        data = r.json()
    except ValueError:
        return f"HTTP {r.status_code}"
    err = data.get("error")
    if isinstance(err, dict):
        return f"HTTP {r.status_code} {err.get('message', '')}".strip()
    return f"HTTP {r.status_code} {err or ''} {data.get('error_description', '')}".strip()


def _check(r: httpx.Response) -> httpx.Response:
    if r.status_code >= 400:
        raise GoogleApiError(f"Google カレンダーの操作に失敗しました（{_error_text(r)}）")
    return r


def body_hash(body: dict) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


class GoogleCalendarSync:
    def __init__(self, settings: Settings, db: Database, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.settings = settings
        self.db = db
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=10.0), transport=transport)
        self._pending: dict[str, tuple[str, str, float]] = {}  # state -> (code_verifier, redirect_uri, 作成時刻)
        self._lock = asyncio.Lock()
        self.error = ""
        self.synced = 0
        self.last_sync: Optional[datetime] = None
        self.on_connected: Optional[Callable[[], None]] = None

    async def aclose(self) -> None:
        await self._http.aclose()

    # ---------- 設定ファイル ----------
    def client_config(self) -> Optional[dict]:
        """Google Cloud でダウンロードした credentials.json（OAuth クライアント）を読む。"""
        path = self.settings.google_credentials_path
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        conf = data.get("installed") or data.get("web") or data
        if not conf.get("client_id"):
            raise ValueError("credentials.json に client_id がありません")
        return conf

    def _load_token(self) -> Optional[dict]:
        path = self.settings.google_token_path
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None

    def _save_token(self, token: dict) -> None:
        path = self.settings.google_token_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(token, ensure_ascii=False, indent=2), encoding="utf-8")

    @property
    def state(self) -> str:
        if not self.settings.google_calendar_enabled:
            return "disabled"
        try:
            conf = self.client_config()
        except Exception:
            return "bad_credentials"
        if conf is None:
            return "no_credentials"
        token = self._load_token()
        if not token or not token.get("refresh_token"):
            return "not_connected"
        return "error" if self.error else "connected"

    # ---------- 連携（OAuth） ----------
    def authorization_url(self, redirect_uri: str) -> str:
        conf = self.client_config()
        if not conf:
            raise GoogleAuthError("credentials.json が見つかりません")
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(24)
        now = _time.time()
        self._pending = {k: v for k, v in self._pending.items() if now - v[2] < 900}
        self._pending[state] = (verifier, redirect_uri, now)
        params = {
            "client_id": conf["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",  # リフレッシュトークンを確実に受け取る
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def has_pending(self, state: str) -> bool:
        return state in self._pending

    async def complete_auth(self, code: Optional[str], state: str, error: Optional[str] = None) -> None:
        entry = self._pending.pop(state, None)
        if entry is None:
            raise GoogleAuthError("連携の手続きが見つかりません。もう一度「連携する」から始めてください")
        if error or not code:
            raise GoogleAuthError(f"Google での許可がキャンセルされました（{error or 'no code'}）")
        verifier, redirect_uri, _ = entry
        conf = self.client_config() or {}
        data = {
            "code": code,
            "client_id": conf.get("client_id", ""),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": verifier,
        }
        if conf.get("client_secret"):
            data["client_secret"] = conf["client_secret"]
        r = await self._http.post(TOKEN_URL, data=data)
        if r.status_code != 200:
            raise GoogleAuthError(f"Google からトークンを受け取れませんでした（{_error_text(r)}）")
        tok = r.json()
        if not tok.get("refresh_token"):
            raise GoogleAuthError("リフレッシュトークンを受け取れませんでした。もう一度連携してください")
        self._save_token(
            {
                "access_token": tok.get("access_token", ""),
                "refresh_token": tok["refresh_token"],
                "expires_at": _time.time() + float(tok.get("expires_in", 0)),
                "scope": tok.get("scope", SCOPE),
            }
        )
        self.error = ""
        log.info("Google カレンダーと連携しました")
        if self.on_connected:
            self.on_connected()

    async def disconnect(self) -> None:
        """連携を解除する（Google 側の許可も取り消す）。作ったカレンダーと予定は Google に残る。"""
        tok = self._load_token()
        if tok and tok.get("refresh_token"):
            try:
                await self._http.post(REVOKE_URL, data={"token": tok["refresh_token"]})
            except httpx.HTTPError:
                pass
        self.settings.google_token_path.unlink(missing_ok=True)
        self.error = ""

    # ---------- アクセストークン ----------
    async def _access_token(self, force_refresh: bool = False) -> str:
        tok = self._load_token()
        if not tok or not tok.get("refresh_token"):
            raise GoogleAuthError("Google カレンダーと連携していません")
        if not force_refresh and tok.get("access_token") and float(tok.get("expires_at", 0)) - 60 > _time.time():
            return tok["access_token"]
        conf = self.client_config() or {}
        data = {"client_id": conf.get("client_id", ""), "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"}
        if conf.get("client_secret"):
            data["client_secret"] = conf["client_secret"]
        r = await self._http.post(TOKEN_URL, data=data)
        if r.status_code in (400, 401):
            raise GoogleAuthError(f"Google との連携が切れました。もう一度連携してください（{_error_text(r)}）")
        _check(r)
        new = r.json()
        tok["access_token"] = new["access_token"]
        tok["expires_at"] = _time.time() + float(new.get("expires_in", 3600))
        if new.get("refresh_token"):
            tok["refresh_token"] = new["refresh_token"]
        self._save_token(tok)
        return tok["access_token"]

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        token = await self._access_token()
        r = await self._http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
        if r.status_code == 401:  # 期限切れなど → 取り直して1回だけやり直す
            token = await self._access_token(force_refresh=True)
            r = await self._http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
        return r

    # ---------- カレンダー ----------
    async def _ensure_calendar(self) -> str:
        cal_id = self.db.get_state(STATE_CALENDAR_ID)
        if cal_id:
            r = await self._request("GET", f"{API}/calendars/{quote(cal_id, safe='')}")
            if r.status_code == 200:
                return cal_id
            if r.status_code not in (403, 404):
                _check(r)
            # カレンダーが削除された／別のアカウントで連携し直した → 作り直して全件登録し直す
            log.warning("Google カレンダーの専用カレンダーが見つからないため作り直します")
            self.db.clear_gcal_links()
        r = _check(
            await self._request(
                "POST",
                f"{API}/calendars",
                json={
                    "summary": self.settings.google_calendar_name,
                    "description": "スケジュール管理アプリが Discord などの連絡から自動で登録した予定",
                    "timeZone": self.settings.timezone,
                },
            )
        )
        cal_id = r.json()["id"]
        self.db.set_state(STATE_CALENDAR_ID, cal_id)
        log.info("Google カレンダーに「%s」を作成しました", self.settings.google_calendar_name)
        return cal_id

    def event_body(self, ev: Event) -> dict:
        tz = self.settings.timezone
        if ev.start_time is None:
            start = {"date": ev.date.isoformat()}
            end = {"date": (ev.date + timedelta(days=1)).isoformat()}
            reminders: dict = {"useDefault": True}
        else:
            s = datetime.combine(ev.date, ev.start_time)
            if ev.end_time and ev.end_time > ev.start_time:
                e = datetime.combine(ev.date, ev.end_time)
            else:
                e = s + timedelta(minutes=self.settings.default_duration_min)
            start = {"dateTime": s.isoformat(timespec="seconds"), "timeZone": tz}
            end = {"dateTime": e.isoformat(timespec="seconds"), "timeZone": tz}
            n = self.settings.google_reminder_min
            reminders = {"useDefault": False, "overrides": [{"method": "popup", "minutes": n}]} if n > 0 else {"useDefault": False}
        lines = [ev.description] if ev.description else []
        origin = " ・ ".join(x for x in (SOURCE_LABELS.get(ev.source, ev.source), ev.source_channel, ev.source_author) if x)
        if origin:
            lines.append(f"連絡元: {origin}")
        if ev.source_url:
            lines.append(f"元メッセージ: {ev.source_url}")
        lines.append("（スケジュール管理アプリが自動で登録）")
        return {
            "summary": ev.title,
            "location": ev.location,
            "description": "\n".join(lines),
            "start": start,
            "end": end,
            "reminders": reminders,
            "extendedProperties": {"private": {"scheduleAppId": str(ev.id)}},
        }

    # ---------- 同期 ----------
    async def sync_once(self) -> None:
        if self.state in ("disabled", "no_credentials", "bad_credentials", "not_connected"):
            return
        async with self._lock:
            try:
                await self._sync()
                self.error = ""
            except GoogleAuthError as e:
                self.error = str(e)
                log.warning("%s", e)
            except httpx.HTTPError as e:
                self.error = f"Google に接続できません（{type(e).__name__}）。次回やり直します"
                log.warning("Google カレンダーに接続できません: %s", e)
            except Exception as e:
                self.error = str(e)
                log.exception("Google カレンダーの同期に失敗しました")

    async def _sync(self) -> None:
        cal = await self._ensure_calendar()
        base = f"{API}/calendars/{quote(cal, safe='')}/events"
        today = datetime.now(self.settings.tz).date()
        desired = {
            e.id: e for e in self.db.list_events(statuses=("confirmed",), date_from=today - timedelta(days=PAST_DAYS))
        }
        for ev in desired.values():
            body = self.event_body(ev)
            h = body_hash(body)
            if ev.gcal_event_id and ev.gcal_hash == h:
                continue  # 変わっていない
            r = None
            if ev.gcal_event_id:
                r = await self._request("PUT", f"{base}/{quote(ev.gcal_event_id, safe='')}", json=body)
                if r.status_code in (404, 410):  # Google カレンダー側で消された → 追加し直す
                    r = None
            if r is None:
                r = await self._request("POST", base, json=body)
            _check(r)
            self.db.set_gcal(ev.id, r.json()["id"], h)
        # 確定でなくなった予定（取り消し・まとめ・却下など）は Google カレンダーから消す
        for ev in self.db.list_gcal_linked():
            if ev.id in desired or ev.status == "confirmed":  # 30日より前の確定済みの予定はそのまま残す
                continue
            await self._delete(base, ev.gcal_event_id)
            self.db.set_gcal(ev.id, None, None)
        # アプリで削除された予定
        for gid in self.db.list_gcal_tombstones():
            await self._delete(base, gid)
            self.db.remove_gcal_tombstone(gid)
        self.synced = len(desired)
        self.last_sync = datetime.now(self.settings.tz)

    async def _delete(self, base: str, gcal_event_id: str) -> None:
        r = await self._request("DELETE", f"{base}/{quote(gcal_event_id, safe='')}")
        if r.status_code not in (200, 204, 404, 410):
            _check(r)

    def status(self) -> dict:
        return {
            "state": self.state,
            "error": self.error,
            "calendar_name": self.settings.google_calendar_name,
            "synced": self.synced,
            "last_sync": self.last_sync.isoformat(timespec="seconds") if self.last_sync else None,
        }
