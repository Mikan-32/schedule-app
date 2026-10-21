"""画面に貼り付けた文章・画像を裏で精査する（POST /api/analyze/jobs）。"""
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.llm import LLMUnavailable
from app.main import create_app
from tests.test_api import FakeLLM

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32  # 中身は問わない（FakeLLM は画像を読まない）


class OfflineLLM(FakeLLM):
    """offline の間は Ollama に接続できない。"""

    def __init__(self, events=None):
        super().__init__(events)
        self.offline = True

    async def extract(self, *args, **kwargs):
        if self.offline:
            raise LLMUnavailable("Ollama に接続できません")
        return await super().extract(*args, **kwargs)


def _client(tmp_path: Path, llm) -> TestClient:
    settings = Settings(db_path=tmp_path / "t.db", discord_token="")
    return TestClient(create_app(settings, llm=llm, start_connectors=False))


def _wait(c: TestClient, job_id: str, states=("done", "failed", "unknown"), timeout=5.0) -> dict:
    end = time.time() + timeout
    while True:
        job = c.get(f"/api/analyze/jobs/{job_id}").json()
        if job["state"] in states or time.time() > end:
            return job
        time.sleep(0.05)


def test_pasted_image_is_analyzed_in_background(tmp_path: Path, future):
    llm = FakeLLM()
    d = future(13).isoformat()
    llm.events = [{"title": "研究室BBQ", "date": d, "start_time": "11:00", "end_time": "15:00", "confidence": 0.9}]
    with _client(tmp_path, llm) as c:
        r = c.post("/api/analyze/jobs", files=[("images", ("bbq.png", PNG, "image/png"))], data={"text": ""})
        assert r.status_code == 202
        job = r.json()
        assert job["id"].startswith("paste-") and job["state"] in ("waiting", "running", "done")

        job = _wait(c, job["id"])
        assert job["state"] == "done"
        assert [x["status"] for x in job["outcome"]["results"]] == ["added"]
        assert llm.calls[0]["text"] == "" and llm.calls[0]["images"][0].data == PNG
        assert [e["title"] for e in c.get("/api/events").json()] == ["研究室BBQ"]
        assert [n["kind"] for n in c.get("/api/notifications").json()] == ["added"]
        # 精査が終わったら、画像はキューに残さない
        assert c.get("/api/status").json()["queue"]["waiting"] == 0
        assert c.get("/api/analyze/jobs").json() == []


def test_paste_with_no_schedule_leaves_a_notification(tmp_path: Path):
    llm = FakeLLM()  # 予定なし
    with _client(tmp_path, llm) as c:
        r = c.post("/api/analyze/jobs", files=[("images", ("a.png", PNG, "image/png")), ("images", ("b.png", PNG, "image/png"))])
        job = _wait(c, r.json()["id"])
        assert job["state"] == "done" and job["outcome"]["results"] == []
        notes = c.get("/api/notifications").json()
        assert [(n["kind"], n["message"]) for n in notes] == [("info", "貼り付けた画像2枚: 予定は見つかりませんでした")]


def test_paste_of_registered_event_says_so(tmp_path: Path, future):
    llm = FakeLLM()
    d = future(5).isoformat()
    with _client(tmp_path, llm) as c:
        c.post("/api/events", json={"title": "定例ミーティング", "date": d, "start_time": "14:00"})
        llm.events = [{"title": "定例ミーティング", "date": d, "start_time": "14:00", "confidence": 0.9}]
        r = c.post("/api/analyze/jobs", data={"text": "定例ミーティングは14時からです"})
        job = _wait(c, r.json()["id"])
        assert [x["status"] for x in job["outcome"]["results"]] == ["duplicate"]
        info = c.get("/api/notifications").json()[0]
        assert info["kind"] == "info" and info["message"].startswith("貼り付けたメッセージ: 既に登録済みの予定でした\n")


def test_paste_waits_until_llm_is_ready(tmp_path: Path, future):
    llm = OfflineLLM([{"title": "歯医者", "date": future(3).isoformat(), "start_time": "10:00", "confidence": 0.9}])
    with _client(tmp_path, llm) as c:
        r = c.post("/api/analyze/jobs", files=[("images", ("a.png", PNG, "image/png"))])
        job_id = r.json()["id"]
        job = _wait(c, job_id, states=("failed", "done"), timeout=0.5)
        assert job["state"] == "waiting" and "接続できません" in job["error"]
        # 画面を開き直しても、終わっていない解析が分かる
        assert [j["id"] for j in c.get("/api/analyze/jobs").json()] == [job_id]

        llm.offline = False
        c.post("/api/llm/refresh")  # つながったら、すぐ精査する
        job = _wait(c, job_id)
        assert job["state"] == "done" and job["outcome"]["results"][0]["status"] == "added"


def test_paste_validation(tmp_path: Path, monkeypatch):
    import app.web.api as api

    with _client(tmp_path, FakeLLM()) as c:
        assert c.post("/api/analyze/jobs", data={"text": "  "}).status_code == 400
        r = c.post("/api/analyze/jobs", files=[("images", ("a.txt", b"hello", "text/plain"))])
        assert r.status_code == 400 and "画像・PDF ではない" in r.json()["detail"]
        five = [("images", (f"{i}.png", PNG, "image/png")) for i in range(5)]
        assert c.post("/api/analyze/jobs", files=five).status_code == 400
        monkeypatch.setattr(api, "MAX_PASTE_IMAGE_BYTES", 10)
        assert c.post("/api/analyze/jobs", files=[("images", ("a.png", PNG, "image/png"))]).status_code == 413
        assert c.get("/api/analyze/jobs/paste-unknown").json()["state"] == "unknown"
        assert c.get("/api/status").json()["queue"]["waiting"] == 0
