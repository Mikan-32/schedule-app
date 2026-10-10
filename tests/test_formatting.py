from datetime import date, time

from app.formatting import format_conflict_message, format_event_line
from app.models import Event


def test_event_line_on_the_hour():
    ev = Event(title="定例ミーティング", date=date(2026, 10, 15), start_time=time(14, 0))
    assert format_event_line(ev) == "10月15日(木)14時から定例ミーティング"


def test_event_line_with_minutes():
    ev = Event(title="歯医者", date=date(2026, 10, 12), start_time=time(9, 30))
    assert format_event_line(ev) == "10月12日(月)9時30分から歯医者"


def test_event_line_all_day():
    ev = Event(title="レポート提出締切", date=date(2026, 10, 20))
    assert format_event_line(ev) == "10月20日(火) 終日 レポート提出締切"


def test_conflict_message():
    new = Event(title="歯医者", date=date(2026, 10, 15), start_time=time(14, 30))
    old = Event(title="定例", date=date(2026, 10, 15), start_time=time(14, 0))
    msg = format_conflict_message(new, [old])
    assert msg.splitlines() == [
        "10月15日(木)は既に予定があります。追加しますか？",
        "新しい予定: 10月15日(木)14時30分から歯医者",
        "既存の予定: 10月15日(木)14時から定例",
    ]
