"""macOS の通知センターのデータベースを読み取り専用で参照する（NotificationConnector の読み取り方式 database）。

場所:
  - macOS 15 (Sequoia) 以降: ~/Library/Group Containers/group.com.apple.usernoted/db2/db
    プライバシー保護の対象なので「フルディスクアクセス」の許可が必要。ターミナルから起動するならターミナルを、
    自動起動（launchd）なら Python を、システム設定 → プライバシーとセキュリティ → フルディスクアクセス でオンにする。
  - macOS 14 以前: $(getconf DARWIN_USER_DIR)com.apple.notificationcenter/db2/db（許可は不要）
中身:
  - record テーブル（1 行 = 1 通知）の data 列がバイナリ plist。req の titl / subt / body がタイトル・サブタイトル・本文
  - app テーブルの identifier がアプリのバンドルID（Discord は com.hnc.Discord）
  - delivered_date は 2001-01-01（UTC）からの秒数
  - WAL 形式なので、使用中で開けないときは db-wal / db-shm ごと一時フォルダにコピーして読む
macOS の版によって形が違っても読めるよう、列の有無を確かめてから読み、plist の形が違っても titl / body を探す。
"""
from __future__ import annotations

import asyncio
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .notifications import NotificationSource, ToastNotification

COCOA_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
# よく使うアプリのバンドルID → 表示名（NOTIFY_APPS の部分一致に使う）
APP_NAMES = {
    "com.hnc.discord": "Discord",
    "com.tinyspeck.slackmacgap": "Slack",
    "com.microsoft.teams2": "Microsoft Teams",
    "com.microsoft.teams": "Microsoft Teams",
    "us.zoom.xos": "Zoom",
    "jp.naver.line.mac": "LINE",
}
# 前回読んだ最新の通知の時刻より、この秒数だけ前から読み直す（同じ時刻に届いた通知の取りこぼしを防ぐ。読んだものは除く）
OVERLAP_SEC = 600.0


def cocoa_to_datetime(value: Any) -> Optional[datetime]:
    try:
        return COCOA_EPOCH + timedelta(seconds=float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def darwin_user_dir() -> str:
    """getconf DARWIN_USER_DIR（/var/folders/xx/yyyy/0/）。取れなければ空。"""
    try:
        value = os.confstr("CS_DARWIN_USER_DIR")
        if value:
            return value
    except (ValueError, OSError, AttributeError):
        pass
    try:
        out = subprocess.run(["getconf", "DARWIN_USER_DIR"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def candidate_paths() -> list[Path]:
    paths = [Path.home() / "Library" / "Group Containers" / "group.com.apple.usernoted" / "db2" / "db"]
    d = darwin_user_dir()
    if d:
        paths.append(Path(d) / "com.apple.notificationcenter" / "db2" / "db")
    return paths


def running_python() -> str:
    """フルディスクアクセスで許可する Python。フレームワーク版の Python は、実際には Python.app の中のものが動いている。"""
    exe = os.path.realpath(sys.executable)
    if sys.platform == "darwin":
        try:
            import ctypes

            libc = ctypes.CDLL(None)
            size = ctypes.c_uint32(4096)
            buf = ctypes.create_string_buffer(size.value)
            if libc._NSGetExecutablePath(buf, ctypes.byref(size)) == 0:
                exe = os.path.realpath(buf.value.decode())
        except Exception:
            pass
    marker = ".app/Contents/MacOS/"
    return exe[: exe.index(marker) + 4] if marker in exe else exe  # .app の中なら .app を許可する


def full_disk_access_hint() -> str:
    return (
        "通知データベースを読む許可がありません。システム設定 → プライバシーとセキュリティ → フルディスクアクセス で、"
        "アプリを起動しているターミナル（自動起動のときは Python: "
        f"{running_python()}）をオンにしてから、アプリを起動し直してください"
    )


# ---------- record.data（バイナリ plist）の読み取り ----------
def _unarchive(obj: Any) -> Any:
    """NSKeyedArchiver 形式（$archiver / $objects / $top）なら普通の dict・list・文字列に直す。それ以外はそのまま。"""
    if not (isinstance(obj, dict) and obj.get("$archiver") == "NSKeyedArchiver" and isinstance(obj.get("$objects"), list)):
        return obj
    objects = obj["$objects"]

    def resolve(v: Any, depth: int = 0) -> Any:
        if depth > 30:
            return None
        if isinstance(v, plistlib.UID):
            v = objects[v.data] if 0 <= v.data < len(objects) else None
            if v == "$null":
                return None
        if isinstance(v, dict):
            if "NS.keys" in v and "NS.objects" in v:
                out = {}
                for k, o in zip(v["NS.keys"], v["NS.objects"]):
                    key = resolve(k, depth + 1)
                    if isinstance(key, (str, int, float, bytes)):
                        out[key] = resolve(o, depth + 1)
                return out
            if "NS.objects" in v:
                return [resolve(o, depth + 1) for o in v["NS.objects"]]
            if "NS.string" in v:
                return v["NS.string"]
            return {k: resolve(x, depth + 1) for k, x in v.items() if k != "$class"}
        if isinstance(v, list):
            return [resolve(x, depth + 1) for x in v]
        return v

    return resolve(obj.get("$top", {}))


def _find_req(obj: Any, depth: int = 0) -> Optional[dict]:
    """タイトル・本文（titl / body）を持つ dict を探す。ふつうは obj["req"]。"""
    if depth > 6:
        return None
    if isinstance(obj, dict):
        req = obj.get("req")
        if isinstance(req, dict) and ("titl" in req or "body" in req):
            return req
        if "titl" in obj or "body" in obj:
            return obj
        children = list(obj.values())
    elif isinstance(obj, list):
        children = obj
    else:
        return None
    for child in children:
        found = _find_req(child, depth + 1)
        if found is not None:
            return found
    return None


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def parse_record(data: Any) -> dict:
    """record.data から {"title", "subtitle", "body", "app", "date"} を取り出す（読めなければ空の dict）。"""
    try:
        obj = _unarchive(plistlib.loads(bytes(data)))
    except Exception:
        return {}
    req = _find_req(obj)
    if req is None:
        return {}
    top = obj if isinstance(obj, dict) else {}
    return {
        "title": _text(req.get("titl")),
        "subtitle": _text(req.get("subt")),
        "body": _text(req.get("body")),
        "app": _text(top.get("app")),
        "date": top.get("date"),
    }


def app_display_name(bundle_id: str) -> str:
    return APP_NAMES.get(bundle_id.lower()) or bundle_id.rsplit(".", 1)[-1]


def record_to_toast(key: str, data: Any, date: Any, bundle_id: Optional[str]) -> Optional[ToastNotification]:
    info = parse_record(data)
    if not info or not (info["title"] or info["body"]):
        return None
    bundle = bundle_id or info["app"]
    created = cocoa_to_datetime(date) or cocoa_to_datetime(info["date"]) or datetime.now(timezone.utc)
    return ToastNotification(
        key=key,
        app_name=app_display_name(bundle) if bundle else "",
        app_id=bundle or "",
        texts=[info["title"]] + ([info["body"]] if info["body"] else []),
        created_at=created,
        subtitle=info["subtitle"],
    )


# ---------- 読み取り方式 ----------
class MacDatabaseSource(NotificationSource):
    """macOS の通知センターのデータベース。"""

    name = "database"

    def __init__(self, path: Optional[Path] = None, candidates: Optional[list[Path]] = None):
        self._explicit = Path(path) if path else None
        self._candidates = candidates
        self.path: Optional[Path] = None
        self._schema: dict = {}
        self._watermark = 0.0  # 読んだ通知の最新の時刻（2001-01-01 からの秒）
        self._seen: dict[str, float] = {}  # 読み直す範囲の中で、もう返した通知

    async def open(self) -> None:
        paths = [self._explicit] if self._explicit else (self._candidates if self._candidates is not None else candidate_paths())
        errors: list[str] = []
        denied = False
        for path in paths:
            try:
                self._schema = await asyncio.to_thread(self._probe, path)
            except PermissionError:
                # ファイルはあるが許可がない（macOS 15 以降でフルディスクアクセスがない）。ここで止める
                # （以前の場所に古いデータベースが残っていても、もう更新されないので読まない）
                denied = True
                break
            except FileNotFoundError:
                errors.append(f"{path}: ありません")
                continue
            except Exception as e:
                errors.append(f"{path}: {e}")
                continue
            self.path = path
            return
        if denied:
            raise PermissionError(full_disk_access_hint())
        raise FileNotFoundError("通知データベースを読めません（" + " / ".join(errors) + "）")

    def _probe(self, path: Path) -> dict:
        os.stat(path)  # ない → FileNotFoundError、プライバシー保護で読めない → PermissionError
        return self._read(path, self._detect_schema)

    @staticmethod
    def _detect_schema(conn: sqlite3.Connection) -> dict:
        def columns(table: str) -> set[str]:
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}

        record, app = columns("record"), columns("app")
        if "data" not in record:
            raise ValueError("通知データベースの形が想定と違います（record テーブルの data 列がありません）")
        return {
            "id": "rec_id" if "rec_id" in record else "rowid",
            "date": next((c for c in ("delivered_date", "request_date", "request_last_date") if c in record), None),
            "uuid": "uuid" in record,
            "join_app": "app_id" in record and {"app_id", "identifier"} <= app,
        }

    def _read(self, path: Path, fn: Callable[[sqlite3.Connection], Any]) -> Any:
        """読み取り専用で開いて fn(conn) を実行する。使用中で開けないときは WAL ごと一時フォルダにコピーして読む。"""
        try:
            conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2)
            try:
                return fn(conn)
            finally:
                conn.close()
        except sqlite3.Error:
            with tempfile.TemporaryDirectory() as tmp:
                for suffix in ("", "-wal", "-shm"):
                    src = Path(str(path) + suffix)
                    try:
                        shutil.copy2(src, Path(tmp) / (path.name + suffix))
                    except FileNotFoundError:
                        if not suffix:
                            raise
                conn = sqlite3.connect(Path(tmp) / path.name)
                try:
                    return fn(conn)
                finally:
                    conn.close()

    def _query(self, since: float) -> list[tuple]:
        s = self._schema
        date = f"r.{s['date']}" if s["date"] else "NULL"
        uuid = "r.uuid" if s["uuid"] else "NULL"
        app, join = ("a.identifier", "LEFT JOIN app a ON a.app_id = r.app_id") if s["join_app"] else ("NULL", "")
        sql = f"SELECT r.{s['id']}, {uuid}, r.data, {date}, {app} FROM record r {join}"
        if s["date"]:
            sql += f" WHERE {date} >= ? ORDER BY {date}"
            params: tuple = (since,)
        else:
            sql += f" ORDER BY r.{s['id']} DESC LIMIT 500"
            params = ()
        return self._read(self.path, lambda conn: conn.execute(sql, params).fetchall())

    async def fetch(self) -> list[ToastNotification]:
        since = self._watermark - OVERLAP_SEC if self._watermark else 0.0
        rows = await asyncio.to_thread(self._query, since)
        result = []
        for rec_id, uuid, data, date, bundle_id in rows:
            if isinstance(uuid, (bytes, bytearray, memoryview)):
                key = f"mac-{bytes(uuid).hex()}"
            else:
                key = f"mac-{uuid}" if uuid else f"mac-{rec_id}-{date}"
            if key in self._seen:
                continue
            when = float(date) if isinstance(date, (int, float)) else self._watermark
            self._seen[key] = when
            self._watermark = max(self._watermark, when)
            toast = record_to_toast(key, data, date, bundle_id)
            if toast:
                result.append(toast)
        if self._watermark:
            cutoff = self._watermark - OVERLAP_SEC * 2
            self._seen = {k: t for k, t in self._seen.items() if t >= cutoff}
        return result
