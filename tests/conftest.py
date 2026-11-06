import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# テストが本物の Google カレンダーや PC の電源設定に触れないようにする（.env より優先される）
os.environ["GOOGLE_CALENDAR"] = "false"
os.environ["PREVENT_SLEEP"] = "off"
os.environ["NOTIFICATIONS"] = "false"  # PC の通知の読み取り（以前の名前 WINDOWS_NOTIFICATIONS より優先される）
os.environ["MY_NAMES"] = ""
os.environ["OTHERS_MODE"] = "skip"
os.environ["MY_GROUPS"] = ""
# 既存のテストは宛先の判定とは関係ないので、宛先にかかわらず登録する（宛先ごとの扱いは test_audience.py で確かめる）
os.environ["AUDIENCE_ME"] = "add"
os.environ["AUDIENCE_ALL"] = "add"
os.environ["AUDIENCE_UNKNOWN"] = "add"
os.environ["LLM_RUNTIME"] = "external"  # テストで同梱の Ollama をダウンロード・起動しない
os.environ["LLM_RECHECK"] = "true"  # .env で聞き直しを止めていても、聞き直しのテストが既定（true）で動くようにする

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

    async def extract(self, text, sent_at, author="", channel="", images=None, existing=None, context=None, reply_to=""):
        self.calls.append(
            {
                "text": text,
                "sent_at": sent_at,
                "images": images or [],
                "existing": existing or [],
                "context": context or [],
                "reply_to": reply_to,
            }
        )
        return ExtractionResult(events=[ExtractedEvent(**e) for e in self.events])


class RecordingSink:
    name = "recording"

    def __init__(self):
        self.added = []
        self.conflicts = []
        self.changed = []
        self.change_pending = []

    async def notify_added(self, event, message):
        self.added.append(message)

    async def notify_conflict(self, event, conflicts, message):
        self.conflicts.append(message)

    async def notify_changed(self, change, event, message):
        self.changed.append(message)

    async def notify_change_pending(self, change, event, message):
        self.change_pending.append(message)


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
