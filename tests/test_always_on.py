"""取りこぼし対策（再精査キュー・スリープ防止）と Google カレンダー同期のテスト。"""
import json
import re
import time as _time
from datetime import datetime, time
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, unquote, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.gcal import SCOPE, GoogleAuthError, GoogleCalendarSync
from app.llm import LLMUnavailable
from app.main import create_app
from app.models import Event, ImageInput, IncomingMessage
from app.power import SleepGuard
from tests.test_api import FakeLLM


def msg(text, mid, images=None):
    return IncomingMessage(source="discord", message_id=mid, text=text, sent_at=datetime.now(Settings().tz), images=images or [])


# ---------- 再精査キュー ----------
async def test_queue_keeps_images(env):
    service, extractor, _, db = env

    async def down(**kwargs):
        raise LLMUnavailable("Ollama が起動していません")

    extractor.extract = down
    out = await service.handle_message(msg("このチラシの日程", "q1", [ImageInput(data=b"\x89PNG-data", mime="image/png", filename="a.png")]))
    assert out.queued
    [(qid, saved)] = db.list_queue()
    assert saved.text == "このチラシの日程" and saved.images[0].data == b"\x89PNG-data" and saved.images[0].filename == "a.png"

    # まだつながらない → 回数に数えずにそのまま待つ
    assert await service.process_queue() == 0
    assert db.queue_counts() == {"waiting": 1, "failed": 0}


async def test_queue_gives_up_after_max_attempts_and_can_retry(env):
    service, extractor, _, db = env

    async def broken(**kwargs):
        raise RuntimeError("壊れた応答")

    extractor.extract = broken
    await service.handle_message(msg("明日10時から会議", "q2"))  # 1回目
    await service.process_queue()  # 2回目
    assert db.queue_counts() == {"waiting": 1, "failed": 0}
    await service.process_queue()  # 3回目 → あきらめる
    assert db.queue_counts() == {"waiting": 0, "failed": 1}
    assert db.retry_failed() == 1 and db.queue_counts()["waiting"] == 1


async def test_manual_analyze_still_raises(env):
    service, extractor, _, db = env

    async def down(**kwargs):
        raise LLMUnavailable("down")

    extractor.extract = down
    with pytest.raises(LLMUnavailable):
        await service.handle_message(msg("明日10時から会議", "q3"), force=True)
    assert db.queue_counts()["waiting"] == 0


# ---------- スリープ防止 ----------
async def test_sleep_guard_modes():
    calls = []
    power = {"ac": True}
    guard = SleepGuard("ac", set_state=calls.append, on_ac_power=lambda: power["ac"])
    guard.apply()
    assert calls == [True] and guard.active
    power["ac"] = False
    guard.apply()
    assert calls == [True, False] and not guard.active
    await guard.stop()
    assert calls == [True, False]

    calls.clear()
    always = SleepGuard("always", set_state=calls.append, on_ac_power=lambda: False)
    always.apply()
    await always.stop()
    assert calls == [True, False]

    off = SleepGuard("off", set_state=calls.append, on_ac_power=lambda: True)
    off.start()
    assert off.status() == {"supported": True, "mode": "off", "active": False}


# ---------- Google カレンダー ----------
class FakeGoogle:
    """Google の OAuth と Calendar API の最低限のまね。"""

    def __init__(self):
        self.calendars: dict[str, str] = {}
        self.events: dict[tuple[str, str], dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.token_requests: list[dict] = []
        self.access_token = "at-0"
        self.fail_refresh = False
        self._n = 0

    def _next(self, prefix):
        self._n += 1
        return f"{prefix}{self._n}"

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if request.url.host == "oauth2.googleapis.com":
            form = dict(parse_qsl(request.content.decode()))
            self.token_requests.append(form)
            if path == "/revoke":
                return httpx.Response(200)
            if form.get("grant_type") == "authorization_code":
                return httpx.Response(200, json={"access_token": self.access_token, "refresh_token": "rt-1", "expires_in": 3600})
            if self.fail_refresh:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
            self.access_token = self._next("at-")
            return httpx.Response(200, json={"access_token": self.access_token, "expires_in": 3600})

        self.calls.append((method, path))
        if request.headers.get("Authorization") != f"Bearer {self.access_token}":
            return httpx.Response(401, json={"error": {"message": "Invalid Credentials"}})
        if path == "/calendar/v3/calendars" and method == "POST":
            cal = f"{self._next('cal')}@group.calendar.google.com"
            self.calendars[cal] = json.loads(request.content)["summary"]
            return httpx.Response(200, json={"id": cal})
        m = re.fullmatch(r"/calendar/v3/calendars/([^/]+)(/events(?:/([^/]+))?)?", path)
        cal = unquote(m[1])
        if cal not in self.calendars:
            return httpx.Response(404, json={"error": {"message": "Not Found"}})
        if not m[2]:
            return httpx.Response(200, json={"id": cal})
        if m[3] is None:
            eid = self._next("ev")
            self.events[(cal, eid)] = json.loads(request.content)
            return httpx.Response(200, json={"id": eid})
        key = (cal, unquote(m[3]))
        if key not in self.events:
            return httpx.Response(404, json={"error": {"message": "Not Found"}})
        if method == "PUT":
            self.events[key] = json.loads(request.content)
            return httpx.Response(200, json={"id": key[1]})
        if method == "DELETE":
            del self.events[key]
            return httpx.Response(204)
        return httpx.Response(405)

    def bodies(self):
        return sorted(self.events.values(), key=lambda b: b["summary"])


def gsettings(tmp_path: Path, **kw) -> Settings:
    creds = tmp_path / "google" / "credentials.json"
    creds.parent.mkdir(parents=True, exist_ok=True)
    creds.write_text(json.dumps({"installed": {"client_id": "cid.apps.googleusercontent.com", "client_secret": "sec"}}))
    return Settings(
        db_path=tmp_path / "t.db",
        discord_token="",
        google_calendar="true",
        google_credentials_path=creds,
        google_token_path=tmp_path / "google" / "token.json",
        google_reminder_min=30,
        **kw,
    )


async def connected_sync(tmp_path):
    settings = gsettings(tmp_path)
    db = Database(":memory:", tz=settings.tz)
    google = FakeGoogle()
    gcal = GoogleCalendarSync(settings, db, transport=httpx.MockTransport(google.handler))
    url = gcal.authorization_url("http://127.0.0.1:8000/")
    await gcal.complete_auth("code-1", parse_qs(urlparse(url).query)["state"][0])
    return settings, db, google, gcal


async def test_oauth_flow(tmp_path):
    settings = gsettings(tmp_path)
    db = Database(":memory:", tz=settings.tz)
    google = FakeGoogle()
    gcal = GoogleCalendarSync(settings, db, transport=httpx.MockTransport(google.handler))
    assert gcal.state == "not_connected"

    url = gcal.authorization_url("http://127.0.0.1:8000/")
    q = parse_qs(urlparse(url).query)
    assert q["scope"] == [SCOPE] and q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"]
    assert q["redirect_uri"] == ["http://127.0.0.1:8000/"]

    with pytest.raises(GoogleAuthError):
        await gcal.complete_auth("code-x", "unknown-state")
    await gcal.complete_auth("code-1", q["state"][0])
    req = google.token_requests[-1]
    assert req["code_verifier"] and req["client_secret"] == "sec" and req["redirect_uri"] == "http://127.0.0.1:8000/"
    assert json.loads(settings.google_token_path.read_text())["refresh_token"] == "rt-1"
    assert gcal.state == "connected"

    await gcal.disconnect()
    assert gcal.state == "not_connected" and google.token_requests[-1] == {"token": "rt-1"}
    await gcal.aclose()


async def test_sync_adds_updates_and_removes(tmp_path, future):
    settings, db, google, gcal = await connected_sync(tmp_path)
    meeting = db.add_event(Event(title="定例ミーティング", date=future(3), start_time=time(14, 0), location="会議室A"))
    deadline = db.add_event(Event(title="レポート提出締切", date=future(5)))
    db.add_event(Event(title="確認待ち", date=future(4), start_time=time(9, 0), status="pending"))

    await gcal.sync_once()
    assert gcal.error == "" and list(google.calendars.values()) == ["スケジュール（自動登録）"]
    deadline_body, meeting_body = google.bodies()
    assert meeting_body["start"] == {"dateTime": f"{future(3).isoformat()}T14:00:00", "timeZone": "Asia/Tokyo"}
    assert meeting_body["end"]["dateTime"] == f"{future(3).isoformat()}T15:00:00"  # 終了不明 → 60分
    assert meeting_body["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]}
    assert meeting_body["location"] == "会議室A"
    assert deadline_body["start"] == {"date": future(5).isoformat()} and deadline_body["end"] == {"date": future(6).isoformat()}
    assert db.get_event(meeting.id).gcal_event_id and gcal.status()["synced"] == 2

    # 変わっていなければ API で書き換えない
    google.calls.clear()
    await gcal.sync_once()
    assert [c[0] for c in google.calls] == ["GET"]

    # 変更 → 更新、取り消し → 削除、アプリで削除 → 削除
    meeting.start_time = time(16, 0)
    db.update_event(meeting)
    deadline.status = "cancelled"
    db.update_event(deadline)
    await gcal.sync_once()
    [body] = google.bodies()
    assert body["start"]["dateTime"].endswith("T16:00:00") and db.get_event(deadline.id).gcal_event_id is None
    db.delete_event(meeting.id)
    await gcal.sync_once()
    assert google.events == {} and db.list_gcal_tombstones() == []
    await gcal.aclose()


async def test_sync_recovers_from_google_side_deletions(tmp_path, future):
    settings, db, google, gcal = await connected_sync(tmp_path)
    ev = db.add_event(Event(title="ゼミ", date=future(2), start_time=time(10, 0)))
    await gcal.sync_once()

    # Google カレンダー側で予定を消した → 次に予定が変わったとき追加し直す
    google.events.clear()
    ev.location = "301教室"
    db.update_event(ev)
    await gcal.sync_once()
    assert [b["location"] for b in google.bodies()] == ["301教室"]

    # 専用カレンダーごと消した → 作り直して登録し直す
    google.calendars.clear()
    google.events.clear()
    await gcal.sync_once()
    assert len(google.calendars) == 1 and [b["summary"] for b in google.bodies()] == ["ゼミ"]
    await gcal.aclose()


async def test_token_refresh_and_revoked_token(tmp_path, future):
    settings, db, google, gcal = await connected_sync(tmp_path)
    db.add_event(Event(title="ゼミ", date=future(2), start_time=time(10, 0)))
    # Google 側でアクセストークンが無効になった → 401 → 取り直してやり直す
    google.access_token = "rotated"
    await gcal.sync_once()
    assert gcal.error == "" and len(google.events) == 1
    assert google.token_requests[-1]["grant_type"] == "refresh_token"

    # 期限切れのトークン → 先に取り直す。取り直せない（取り消された） → 再連携が必要
    token = json.loads(settings.google_token_path.read_text())
    token["expires_at"] = _time.time() - 10
    settings.google_token_path.write_text(json.dumps(token))
    google.fail_refresh = True
    await gcal.sync_once()
    assert gcal.state == "error" and "もう一度連携" in gcal.error
    await gcal.aclose()


def test_api_google_flow(tmp_path, future):
    settings = gsettings(tmp_path)
    google = FakeGoogle()
    llm = FakeLLM()
    app = create_app(settings, llm=llm, start_connectors=False, gcal_transport=httpx.MockTransport(google.handler))
    with TestClient(app, follow_redirects=False) as c:
        assert c.get("/api/status").json()["google"]["state"] == "not_connected"
        r = c.get("/api/google/login")
        assert r.status_code == 307 and r.headers["location"].startswith("https://accounts.google.com/")
        state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]

        page = c.get(f"/?code=code-1&state={state}")
        assert page.status_code == 200 and "連携しました" in page.text
        assert c.get("/").status_code == 200  # 通常の画面

        c.post("/api/events", json={"title": "定例", "date": future(3).isoformat(), "start_time": "14:00"})
        st = c.post("/api/google/sync").json()
        assert st["state"] == "connected" and st["synced"] == 1
        assert [b["summary"] for b in google.bodies()] == ["定例"]
        assert c.get("/api/events").json()[0]["gcal_synced"] is True

        assert c.post("/api/google/disconnect").json()["state"] == "not_connected"


def test_api_queue_status_and_retry(tmp_path):
    settings = Settings(db_path=tmp_path / "t.db", discord_token="")
    with TestClient(create_app(settings, llm=FakeLLM(), start_connectors=False)) as c:
        db = c.app.state.ctx.db
        db.enqueue_message(msg("明日10時", "x1"), "down", count_attempt=True)
        db.queue_failed(db.list_queue()[0][0], "broken", max_attempts=1)
        st = c.get("/api/status").json()
        assert st["queue"] == {"waiting": 0, "failed": 1} and st["google"] == {"state": "disabled"}
        assert c.post("/api/queue/retry").json() == {"retried": 1}
