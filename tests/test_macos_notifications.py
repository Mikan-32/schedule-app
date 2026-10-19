"""macOS の通知センターのデータベースの読み取りのテスト（Mac 以外でも動くよう、同じ形のデータベースを作って読む）。"""
import os
import plistlib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.connectors import macos_notifications as mac
from app.connectors.macos_notifications import COCOA_EPOCH, MacDatabaseSource, parse_record
from app.connectors.notifications import NotificationConnector

UID = plistlib.UID


def cocoa(dt: datetime) -> float:
    return (dt - COCOA_EPOCH).total_seconds()


def blob(title, body, subtitle=None, app="com.hnc.Discord") -> bytes:
    """record.data と同じ形のバイナリ plist。"""
    req = {"titl": title, "body": body, "iden": "1", "thre": "t"}
    if subtitle:
        req["subt"] = subtitle
    return plistlib.dumps({"app": app, "req": req, "date": 0.0, "styl": 1, "uuid": b"\x00" * 16}, fmt=plistlib.FMT_BINARY)


def keyed_archive(title, body, app="com.hnc.Discord") -> bytes:
    """NSKeyedArchiver 形式（macOS の版によってはこちらで保存されるかもしれないので、念のため読めるようにしておく）。"""
    nsdict = {"$classname": "NSDictionary", "$classes": ["NSDictionary", "NSObject"]}
    objects = [
        "$null",
        {"NS.keys": [UID(2), UID(3)], "NS.objects": [UID(4), UID(5)], "$class": UID(8)},
        "app",
        "req",
        app,
        {"NS.keys": [UID(6), UID(7)], "NS.objects": [UID(9), UID(10)], "$class": UID(8)},
        "titl",
        "body",
        nsdict,
        title,
        body,
    ]
    archive = {"$archiver": "NSKeyedArchiver", "$version": 100000, "$top": {"root": UID(1)}, "$objects": objects}
    return plistlib.dumps(archive, fmt=plistlib.FMT_BINARY)


def make_db(path: Path) -> None:
    """macOS 15 の通知センターのデータベースと同じ形（使う列だけでなく、ほかの列・テーブルも作る）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(
        """CREATE TABLE app (app_id INTEGER PRIMARY KEY, identifier VARCHAR, badge INTEGER NULL);
           CREATE TABLE record (rec_id INTEGER PRIMARY KEY, app_id INTEGER, uuid BLOB, data BLOB, request_date REAL,
               request_last_date REAL, delivered_date REAL, presented Bool, style INTEGER, snooze_fire_date REAL);
           CREATE TABLE dbinfo (key VARCHAR, value VARCHAR);
           INSERT INTO app (app_id, identifier) VALUES (1, 'com.hnc.Discord'), (2, 'com.tinyspeck.slackmacgap');"""
    )
    conn.commit()
    conn.close()


def add(path: Path, rec_id, app_id, data, when: datetime) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO record (rec_id, app_id, uuid, data, request_date, delivered_date, presented, style) VALUES (?,?,?,?,?,?,1,1)",
        (rec_id, app_id, os.urandom(16), data, cocoa(when), cocoa(when)),
    )
    conn.commit()
    conn.close()


def test_parse_record():
    assert parse_record(blob("山田 (#連絡, 研究室)", "15時から", subtitle="サブ")) == {
        "title": "山田 (#連絡, 研究室)", "subtitle": "サブ", "body": "15時から", "app": "com.hnc.Discord", "date": 0.0}
    keyed = parse_record(keyed_archive("佐藤", "明日10時から"))
    assert (keyed["title"], keyed["body"]) == ("佐藤", "明日10時から")
    assert parse_record(b"not a plist") == {} and parse_record(plistlib.dumps({"x": 1})) == {}


async def test_reads_new_notifications(tmp_path):
    db = tmp_path / "db"
    make_db(db)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    add(db, 1, 1, blob("山田 (#連絡, 研究室)", "来週水曜15時からゼミです"), now - timedelta(minutes=1))
    add(db, 2, 2, blob("#general", "明日10時から", subtitle="田中", app="com.tinyspeck.slackmacgap"), now)
    src = MacDatabaseSource(db)
    await src.open()
    first = await src.fetch()
    assert [(t.app_name, t.app_id, t.title, t.body, t.subtitle) for t in first] == [
        ("Discord", "com.hnc.Discord", "山田 (#連絡, 研究室)", "来週水曜15時からゼミです", ""),
        ("Slack", "com.tinyspeck.slackmacgap", "#general", "明日10時から", "田中"),
    ]
    assert abs((first[1].created_at - now).total_seconds()) < 1
    assert await src.fetch() == []  # 前回以降の新着だけ

    # 通知を消すと番号（rec_id）が使い回されることがあるが、新しい通知として読む
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM record WHERE rec_id = 2")
    conn.commit()
    conn.close()
    add(db, 2, 1, keyed_archive("佐藤", "明日の打ち合わせは14時から"), now + timedelta(seconds=5))
    [t] = await src.fetch()
    assert (t.app_id, t.title, t.body) == ("com.hnc.Discord", "佐藤", "明日の打ち合わせは14時から")


async def test_reads_other_schema(tmp_path):
    # 列が少ない・app テーブルがない版でも読む（アプリは plist の app から）
    db = tmp_path / "db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE record (rec_id INTEGER PRIMARY KEY, data BLOB, request_date REAL)")
    conn.execute("INSERT INTO record (data, request_date) VALUES (?, ?)", (blob("山田", "15時から"), cocoa(datetime.now(timezone.utc))))
    conn.commit()
    conn.close()
    src = MacDatabaseSource(db)
    await src.open()
    [t] = await src.fetch()
    assert (t.app_name, t.app_id, t.title) == ("Discord", "com.hnc.Discord", "山田")

    other = tmp_path / "other.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE something (x)")
    conn.close()
    with pytest.raises(FileNotFoundError, match="想定と違います"):
        await MacDatabaseSource(other).open()


async def test_reads_copy_while_locked(tmp_path):
    # 通知センターが書き込み中で開けないときは、WAL ごとコピーして読む
    db = tmp_path / "db"
    make_db(db)
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA locking_mode=EXCLUSIVE")
    writer.execute("INSERT INTO record (app_id, uuid, data, delivered_date) VALUES (1, ?, ?, ?)",
                   (os.urandom(16), blob("山田", "15時から"), cocoa(datetime.now(timezone.utc))))
    writer.commit()
    try:
        src = MacDatabaseSource(db)
        await src.open()
        assert [t.title for t in await src.fetch()] == ["山田"]
    finally:
        writer.close()


async def test_finds_database_in_older_location(tmp_path):
    old = tmp_path / "var" / "folders" / "0" / "com.apple.notificationcenter" / "db2" / "db"
    make_db(old)
    src = MacDatabaseSource(candidates=[tmp_path / "Group Containers" / "db2" / "db", old])
    await src.open()
    assert src.path == old
    with pytest.raises(FileNotFoundError, match="読めません.*ありません"):
        await MacDatabaseSource(candidates=[tmp_path / "none"]).open()


async def test_permission_error_explains_full_disk_access(tmp_path, monkeypatch):
    db = tmp_path / "Group Containers" / "db2" / "db"
    make_db(db)
    old = tmp_path / "old" / "db"
    make_db(old)  # macOS 14 から更新した Mac に残っている、もう更新されない古いデータベース
    real_stat = os.stat

    def denied(path, *args, **kwargs):  # プライバシー保護で読めない（macOS 15 以降でフルディスクアクセスがないとき）
        if str(path) == str(db):
            raise PermissionError(1, "Operation not permitted")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(mac.os, "stat", denied)
    with pytest.raises(PermissionError, match="フルディスクアクセス"):
        await MacDatabaseSource(candidates=[db, old]).open()  # 古いほうを読んで「監視中」にならない


def test_candidate_paths_and_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(mac, "darwin_user_dir", lambda: "/var/folders/ab/cd/0/")
    assert mac.candidate_paths() == [
        tmp_path / "Library" / "Group Containers" / "group.com.apple.usernoted" / "db2" / "db",
        Path("/var/folders/ab/cd/0/com.apple.notificationcenter/db2/db"),
    ]
    # フレームワーク版の Python は Python.app の中のものが動いているので、.app を許可してもらう
    exe = "/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app/Contents/MacOS/Python"
    monkeypatch.setattr(sys, "executable", exe)
    monkeypatch.setattr(sys, "platform", "linux")  # Mac 以外では _NSGetExecutablePath を使わない
    monkeypatch.setattr(mac.os.path, "realpath", lambda p: p)
    assert mac.running_python() == "/Library/Frameworks/Python.framework/Versions/3.12/Resources/Python.app"
    hint = mac.full_disk_access_hint()
    assert "フルディスクアクセス" in hint and "Python.app" in hint


async def test_connector_with_mac_database(env, tmp_path, future):
    service, extractor, sink, events_db = env
    service.settings.notify_apps = ["Discord"]
    extractor.events = [{"title": "ゼミ", "date": future(3).isoformat(), "start_time": "15:00", "confidence": 0.9}]
    db = tmp_path / "db"
    make_db(db)
    now = datetime.now(timezone.utc)
    add(db, 1, 1, blob("山田", "昨日の件、明日10時からで", subtitle="#連絡, 研究室"), now - timedelta(hours=2))  # 起動前の古い通知
    add(db, 2, 1, blob("山田", "来週水曜15時からゼミです", subtitle="#連絡, 研究室"), now)
    add(db, 3, 2, blob("#general", "明日10時から", app="com.tinyspeck.slackmacgap"), now)  # 対象外のアプリ
    conn = NotificationConnector(service.settings, service, sources=[MacDatabaseSource(db)])
    await conn.start()
    conn._task.cancel()  # ループは使わず poll_once を直接呼ぶ
    queued = await conn.poll_once()
    assert [(m.author, m.channel, m.text) for m in queued] == [("山田", "Discord 研究室 #連絡", "来週水曜15時からゼミです")]
    for job in list(conn._jobs):
        await job
    assert [e.title for e in events_db.list_events()] == ["ゼミ"]
    assert await conn.poll_once() == []
    await conn.stop()
