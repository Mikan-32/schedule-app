import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.models import ExtractedEvent, ExtractionResult  # noqa: E402
from app.notifier import Notifier  # noqa: E402
from app.service import ScheduleService  # noqa: E402


class FakeExtractor:
    """LLM の代わりに決まった結果を返す。"""

    def __init__(self, events=None):
        self.events = events or []
        self.calls = []

    async def extract(self, text, sent_at, author="", channel="", images=None):
        self.calls.append({"text": text, "sent_at": sent_at, "images": images or []})
        return ExtractionResult(events=[ExtractedEvent(**e) for e in self.events])


class RecordingSink:
    name = "recording"

    def __init__(self):
        self.added = []
        self.conflicts = []

    async def notify_added(self, event, message):
        self.added.append(message)

    async def notify_conflict(self, event, conflicts, message):
        self.conflicts.append(message)


@pytest.fixture
def settings():
    return Settings(db_path=Path(":memory:"), discord_token="", conflict_mode="overlap", prefilter=True)


@pytest.fixture
def env(settings):
    db = Database(":memory:", tz=settings.tz)
    extractor = FakeExtractor()
    notifier = Notifier(db)
    sink = RecordingSink()
    notifier.register(sink)
    service = ScheduleService(settings, db, extractor, notifier)
    return service, extractor, sink, db


@pytest.fixture
def future():
    """今日から n 日後の date を返す関数。"""
    today = datetime.now(Settings().tz).date()
    return lambda n: today + timedelta(days=n)
