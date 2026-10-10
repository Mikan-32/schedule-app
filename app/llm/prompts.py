"""予定抽出用プロンプト。"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Optional

from ..formatting import WEEKDAYS_JA
from ..models import EXTRACTION_SCHEMA, Event

SYSTEM_PROMPT = """あなたはチャットメッセージ（と添付画像）から予定・スケジュールを抽出するアシスタントです。
次のルールに厳密に従い、指定のJSONのみを出力してください。

# 抽出対象
- 日付を特定できる予定・会議・イベント・締切・約束のみ。
  （「明日」「来週水曜」「15日」など相対的・省略された表現も可）
- 雑談、過去の出来事の報告、日付を特定できないもの、仮定や検討中の話は抽出しない。
- 該当がなければ events は空配列 [] にする。
- 1つのメッセージに複数の予定があれば、すべて別々に抽出する。

# 新しい予定か、登録済みの予定の変更・取り消しか（action）
- 【登録済みの予定】に、すでに登録されている今後の予定が「#番号 日付 時刻 タイトル」の形で載っている。
- add: 新しい予定。登録済みの予定と同じものを改めて案内・リマインドしているだけの場合も add のままでよい。
- update: 登録済みの予定の日付・時刻・場所・内容が変わるという連絡。
  （「延期」「変更」「リスケ」「前倒し」「○日に変更」「開始を△時にずらす」「場所が××に変わった」など）
- cancel: 登録済みの予定がなくなるという連絡。（「中止」「キャンセル」「なくなった」「取りやめ」「休講」など）
- 「中止になるかも」「変更を検討中」など、まだ決まっていない話は抽出しない。
- update / cancel のとき:
  - target_id: 対象の予定の #番号（数字のみ）。【登録済みの予定】に見当たらなければ 0。
  - target_title / target_date: 対象の予定の元のタイトルと元の日付(YYYY-MM-DD)。分からなければ ""。
  - update の title〜description には「変更後」の内容を書く。変わらない項目は登録済みの予定の値をそのまま写す。
  - cancel の title〜description には取り消す予定の内容を書く。
- add のときは target_id を 0、target_title と target_date を "" にする。

# 各項目
- title: 予定の内容を簡潔に（20文字以内目安）。例: 「定例ミーティング」「歯医者」「レポート提出締切」
- date: YYYY-MM-DD。相対的な表現は【基準日時】から計算し、【参照カレンダー】で曜日を必ず確認する。
  年が省略されている場合は、基準日以降で最も近い日付にする。
- start_time / end_time: 24時間制 HH:MM。「午後3時」→15:00、「正午」「お昼」→12:00。
  時刻が明示されていない場合は空文字 ""。終了時刻が不明なら end_time は ""。
- location: 場所・会議URL（Zoom/Meet等）。なければ ""。
- description: 持ち物・補足など。なければ ""。
- confidence: これが実際に予定として登録すべきものである確信度（0〜1）。
  update / cancel の場合は、その変更・取り消しが確定していて、対象の予定も合っている確信度。

# 画像
- 画像（スクリーンショット、チラシ、招待状、時間割など）が添付されている場合は、画像内の文字も読み取って判断する。

# 出力JSONスキーマ
""" + json.dumps(EXTRACTION_SCHEMA, ensure_ascii=False)


def build_calendar(base: datetime, days: int = 28) -> str:
    labels = {0: "今日", 1: "明日", 2: "明後日"}
    # 今週・来週の区切り（月曜始まり）
    this_monday = (base - timedelta(days=base.weekday())).date()
    lines = []
    for i in range(days):
        d = (base + timedelta(days=i)).date()
        week_offset = (d - this_monday).days // 7
        week_label = {0: "今週", 1: "来週", 2: "再来週"}.get(week_offset, "")
        extra = " ".join(x for x in (labels.get(i, ""), week_label) if x)
        lines.append(f"{d.isoformat()}({WEEKDAYS_JA[d.weekday()]}) {extra}".rstrip())
    return "\n".join(lines)


def format_existing(events: Optional[list[Event]]) -> str:
    """変更・取り消しの対象を特定できるよう、登録済みの予定を一覧にする。"""
    if not events:
        return "（なし）"
    lines = []
    for ev in events:
        if ev.start_time is None:
            when = "終日"
        else:
            when = ev.start_time.strftime("%H:%M") + (f"-{ev.end_time.strftime('%H:%M')}" if ev.end_time else "")
        line = f"#{ev.id} {ev.date.isoformat()}({WEEKDAYS_JA[ev.date.weekday()]}) {when} {ev.title}"
        if ev.location:
            line += f" @{ev.location}"
        lines.append(line)
    return "\n".join(lines)


def build_user_prompt(
    text: str,
    sent_at: datetime,
    author: str = "",
    channel: str = "",
    image_count: int = 0,
    existing: Optional[list[Event]] = None,
) -> str:
    base = f"{sent_at.strftime('%Y-%m-%d')}({WEEKDAYS_JA[sent_at.weekday()]}) {sent_at.strftime('%H:%M')}"
    parts = [
        f"【基準日時】{base}（メッセージ送信日時, {sent_at.tzname() or ''}）",
    ]
    if author:
        parts.append(f"【送信者】{author}")
    if channel:
        parts.append(f"【チャンネル】{channel}")
    if image_count:
        parts.append(f"【添付画像】{image_count}枚")
    parts.append("【参照カレンダー】\n" + build_calendar(sent_at))
    parts.append("【登録済みの予定】\n" + format_existing(existing))
    parts.append("【メッセージ本文】\n" + (text.strip() or "（本文なし。画像のみ）"))
    parts.append("上記から予定を抽出し、JSONで出力してください。")
    return "\n\n".join(parts)
