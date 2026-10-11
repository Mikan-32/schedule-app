from datetime import date
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.holidays import holidays_between, holidays_in_year
from app.main import create_app

from tests.test_api import FakeLLM


def _md(year, items):
    return {date(year, m, d): name for (m, d), name in items.items()}


# 内閣府「国民の祝日について」（https://www8.cao.go.jp/chosei/shukujitsu/gaiyou.html）の掲載どおり。
# 内閣府の表記は振替休日・国民の休日とも「休日」
OFFICIAL = {
    2026: _md(2026, {
        (1, 1): "元日", (1, 12): "成人の日", (2, 11): "建国記念の日", (2, 23): "天皇誕生日", (3, 20): "春分の日",
        (4, 29): "昭和の日", (5, 3): "憲法記念日", (5, 4): "みどりの日", (5, 5): "こどもの日", (5, 6): "休日",
        (7, 20): "海の日", (8, 11): "山の日", (9, 21): "敬老の日", (9, 22): "休日", (9, 23): "秋分の日",
        (10, 12): "スポーツの日", (11, 3): "文化の日", (11, 23): "勤労感謝の日",
    }),
    2027: _md(2027, {
        (1, 1): "元日", (1, 11): "成人の日", (2, 11): "建国記念の日", (2, 23): "天皇誕生日", (3, 21): "春分の日",
        (3, 22): "休日", (4, 29): "昭和の日", (5, 3): "憲法記念日", (5, 4): "みどりの日", (5, 5): "こどもの日",
        (7, 19): "海の日", (8, 11): "山の日", (9, 20): "敬老の日", (9, 23): "秋分の日", (10, 11): "スポーツの日",
        (11, 3): "文化の日", (11, 23): "勤労感謝の日",
    }),
    2025: _md(2025, {
        (1, 1): "元日", (1, 13): "成人の日", (2, 11): "建国記念の日", (2, 23): "天皇誕生日", (2, 24): "休日",
        (3, 20): "春分の日", (4, 29): "昭和の日", (5, 3): "憲法記念日", (5, 4): "みどりの日", (5, 5): "こどもの日",
        (5, 6): "休日", (7, 21): "海の日", (8, 11): "山の日", (9, 15): "敬老の日", (9, 23): "秋分の日",
        (10, 13): "スポーツの日", (11, 3): "文化の日", (11, 23): "勤労感謝の日", (11, 24): "休日",
    }),
}


def _as_official(days):
    return {d: ("休日" if n in ("振替休日", "国民の休日") else n) for d, n in days.items()}


def test_matches_cabinet_office_list():
    for year, expected in OFFICIAL.items():
        assert _as_official(holidays_in_year(year)) == expected, year


def test_substitute_and_sandwiched_names():
    h = holidays_in_year(2026)
    assert h[date(2026, 5, 6)] == "振替休日"  # 5/3（日）の振替
    assert h[date(2026, 9, 22)] == "国民の休日"  # 敬老の日と秋分の日にはさまれた日
    assert holidays_in_year(2027)[date(2027, 3, 22)] == "振替休日"  # 春分の日（日）の振替


def test_olympic_special_years():
    h2020, h2021 = holidays_in_year(2020), holidays_in_year(2021)
    assert h2020[date(2020, 7, 24)] == "スポーツの日" and date(2020, 10, 12) not in h2020
    assert h2021[date(2021, 8, 8)] == "山の日" and h2021[date(2021, 8, 9)] == "振替休日"


def test_out_of_range_years_have_no_holidays():
    assert holidays_in_year(2019) == {} and holidays_in_year(2100) == {}


def test_between_spans_years():
    got = holidays_between(date(2026, 12, 27), date(2027, 1, 16))
    assert got == [(date(2027, 1, 1), "元日"), (date(2027, 1, 11), "成人の日")]


def test_holidays_api(tmp_path: Path):
    settings = Settings(db_path=tmp_path / "t.db", discord_token="")
    with TestClient(create_app(settings, llm=FakeLLM(), start_connectors=False)) as c:
        res = c.get("/api/holidays", params={"date_from": "2026-09-27", "date_to": "2026-11-07"})
        assert res.json() == [{"date": "2026-10-12", "name": "スポーツの日"}, {"date": "2026-11-03", "name": "文化の日"}]
        assert c.get("/api/holidays", params={"date_from": "2026-11-01", "date_to": "2026-10-01"}).status_code == 400
        assert c.get("/api/holidays", params={"date_from": "2026-01-01", "date_to": "2030-01-01"}).status_code == 400
