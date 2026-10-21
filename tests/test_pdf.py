"""貼り付けた PDF の読み取り（app/pdf_reader.py と POST /api/analyze/jobs）。"""
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.llm.prompts import SYSTEM_PROMPT
from app.main import create_app
from app.pdf_reader import PdfError, read_pdf
from tests.pdf_maker import Page, make_pdf, shift_calendar
from tests.test_api import FakeLLM

pytest.importorskip("pdfplumber")

ENTRIES = {
    2: ["Sumino 9:30 15:15"],
    3: ["Masatani 16:00 19:15", "Kishimoto 14:30 19:15"],
    14: ["Masatani 9:30 17:15"],
    30: ["Goto 9:30 15:15"],
}
SHIFT = shift_calendar(ENTRIES, labels={4: "PG"}, hidden={2: ["Masatani 0:00 0:00"], 3: ["Ando 0:00 0:00"]})


def test_calendar_is_organized_by_day():
    text = read_pdf(SHIFT, "2026年11月シフト.pdf", ["Masatani"]).text
    assert text.startswith("【PDF「2026年11月シフト.pdf」の内容")
    lines = text.splitlines()
    # 表の列と日付を LLM に対応させなくて済むよう、日ごとに 1 行。利用者の名前の欄に ★、時刻は「〜」でつなぐ
    assert "2026-11-02(月): Sumino 9:30〜15:15" in lines
    assert "2026-11-03(火): ★Masatani 16:00〜19:15 / Kishimoto 14:30〜19:15" in lines
    assert "2026-11-04(水): PG" in lines
    assert "2026-11-14(土): ★Masatani 9:30〜17:15" in lines
    assert "2026-11-30(月): Goto 9:30〜15:15" in lines
    # 背景と同じ色で隠した文字（Excel の 0:00 など）は読まない
    assert "0:00" not in text and "Ando" not in text
    # 見出しの曜日は日付の行に入れない
    assert "Mon" not in text


def test_dates_need_year_and_month_that_match_the_weekdays():
    # 年月が分からなければ「3日」のまま
    assert "\n3日: ★Masatani 16:00〜19:15" in read_pdf(SHIFT, "shift.pdf", ["Masatani"]).text
    # ファイル名の年月が表の曜日と合わない（10 月 3 日は土曜）なら、日付に直さない
    assert "\n3日: ★Masatani" in read_pdf(SHIFT, "2026年10月シフト.pdf", ["Masatani"]).text
    # 名前が設定されていなければ ★ は付けない
    assert "★" not in read_pdf(SHIFT, "2026年11月シフト.pdf", []).text


def test_plain_text_pdf():
    p = Page()
    p.text(50, 50, "Seminar on 2026-11-20 14:00 at Room 301")
    content = read_pdf(make_pdf([p]), "notice.pdf")
    assert "Seminar on 2026-11-20 14:00 at Room 301" in content.text and content.images == []


def test_pdf_without_text_is_rendered_as_image():
    p = Page()
    p.fill(100, 100, 300, 200, (0.2, 0.4, 0.8))  # スキャンした PDF のように文字データがない
    content = read_pdf(make_pdf([p, p]), "scan.pdf")
    assert content.text == "" and len(content.images) == 2 and content.pages == 2
    assert content.images[0].mime == "image/png" and content.images[0].data[:4] == b"\x89PNG"


def test_broken_pdf():
    with pytest.raises(PdfError):
        read_pdf(b"%PDF-1.4 not really", "broken.pdf")


def test_prompt_explains_shift_tables():
    assert "シフト表" in SYSTEM_PROMPT and "★" in SYSTEM_PROMPT


# ---------- API ----------
def _client(tmp_path: Path, llm, **kw) -> TestClient:
    settings = Settings(db_path=tmp_path / "t.db", discord_token="", **kw)
    return TestClient(create_app(settings, llm=llm, start_connectors=False))


def _wait(c, job_id, timeout=5.0):
    end = time.time() + timeout
    while True:
        job = c.get(f"/api/analyze/jobs/{job_id}").json()
        if job["state"] not in ("waiting", "running") or time.time() > end:
            return job
        time.sleep(0.05)


def test_pasted_pdf_is_read_as_text(tmp_path: Path):
    llm = FakeLLM()
    with _client(tmp_path, llm, my_names=["Masatani"]) as c:
        r = c.post("/api/analyze/jobs", data={"text": "11月のシフトです"},
                   files=[("images", ("2026年11月シフト.pdf", SHIFT, "application/pdf"))])
        assert r.status_code == 202
        job = _wait(c, r.json()["id"])
        assert job["state"] == "done"
        call = llm.calls[0]
        assert call["images"] == []  # 文字データがあるので画像にはしない
        assert call["text"].startswith("11月のシフトです\n\n【PDF「2026年11月シフト.pdf」の内容")
        assert "2026-11-03(火): ★Masatani 16:00〜19:15" in call["text"]
        # 予定がなければ「貼り付けた PDF」と通知に残る
        assert c.get("/api/notifications").json()[0]["message"] == "貼り付けた PDF: 予定は見つかりませんでした"


def test_pdf_found_by_file_name_and_scanned_pages_become_images(tmp_path: Path):
    llm = FakeLLM()
    p = Page()
    p.fill(100, 100, 300, 200, (0.2, 0.4, 0.8))
    with _client(tmp_path, llm) as c:
        # ブラウザによっては PDF の種類が application/octet-stream になる
        r = c.post("/api/analyze/jobs", files=[("images", ("scan.PDF", make_pdf([p]), "application/octet-stream"))])
        assert _wait(c, r.json()["id"])["state"] == "done"
        assert len(llm.calls[0]["images"]) == 1


def test_pdf_errors(tmp_path: Path, monkeypatch):
    import app.web.api as api
    from app.pdf_reader import PdfUnavailable

    with _client(tmp_path, FakeLLM()) as c:
        r = c.post("/api/analyze/jobs", files=[("images", ("broken.pdf", b"%PDF-1.4 broken", "application/pdf"))])
        assert r.status_code == 400 and "broken.pdf" in r.json()["detail"]

        def missing(*a, **k):
            raise PdfUnavailable("PDF を読むライブラリが入っていません")

        monkeypatch.setattr(api, "read_pdf", missing)
        r = c.post("/api/analyze/jobs", files=[("images", ("a.pdf", SHIFT, "application/pdf"))])
        assert r.status_code == 501 and "ライブラリ" in r.json()["detail"]
        assert c.get("/api/status").json()["queue"]["waiting"] == 0
