from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.conftest import FakeExtractor


class FakeLLM(FakeExtractor):
    async def refresh_selection(self):
        from app.llm.ollama_client import ModelSelection

        return ModelSelection(text_model="fake", vision_model="fake")

    def status(self):
        return {"available": True, "text_model": "fake", "vision_model": "fake", "installed": ["fake"], "error": "", "base_url": ""}

    async def aclose(self):
        pass


def test_api_flow(tmp_path: Path, future):
    settings = Settings(db_path=tmp_path / "t.db", discord_token="")
    llm = FakeLLM()
    d = future(6).isoformat()
    with TestClient(create_app(settings, llm=llm, start_connectors=False)) as c:
        assert c.get("/").status_code == 200
        r = c.post("/api/events", json={"title": "定例", "date": d, "start_time": "14:00", "end_time": "15:00"})
        assert r.json()["status"] == "added"

        llm.events = [{"title": "歯医者", "date": d, "start_time": "14:30", "confidence": 0.9}]
        r = c.post("/api/analyze", data={"text": "14時半から歯医者"})
        assert r.status_code == 200 and r.json()["results"][0]["status"] == "pending"

        st = c.get("/api/status").json()
        assert st["pending_count"] == 1 and st["unread_count"] == 2

        pending = c.get("/api/pending").json()
        eid = pending[0]["event"]["id"]
        assert pending[0]["conflicts"][0]["title"] == "定例"
        assert c.post(f"/api/events/{eid}/confirm").json()["status"] == "confirmed"
        assert [e["title"] for e in c.get("/api/events").json()] == ["定例", "歯医者"]

        kinds = [n["kind"] for n in c.get("/api/notifications").json()]
        assert kinds == ["added", "conflict", "added"]
        assert c.delete(f"/api/events/{eid}").json()["ok"]
