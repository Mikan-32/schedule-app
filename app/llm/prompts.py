"""予定抽出用プロンプト。"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import date, datetime, timedelta
from typing import Optional

from ..formatting import WEEKDAYS_JA
from ..models import EXTRACTION_SCHEMA, ContextMessage, Event

SYSTEM_PROMPT = """あなたはチャットメッセージ（と添付画像）から予定・スケジュールを抽出するアシスタントです。
次のルールに厳密に従い、指定のJSONのみを出力してください。

# 抽出対象
- 日付を特定できる予定・会議・イベント・締切・約束・誘い。
  （「明日」「来週水曜」「15日」など相対的・省略された表現も可）
- 誘いや提案（「明日遊ぼう」「土曜に飲みに行かない？」「14日にご飯行こう」など）も、日付を特定できれば予定として抽出する。
  返事をしたかどうかは問わない（利用者の返事はこちらからは見えない）。
- 雑談、過去の出来事の報告、日付を特定できないもの、仮定の話（「もし晴れたら」など）、
  候補日が複数あってまだ決まっていない話（「15日と16日どっちがいい？」）は抽出しない。
- 該当がなければ events は空配列 [] にする。
- 1つのメッセージに複数の予定があれば、すべて別々に抽出する。

# 文脈（直前のやり取り・返信先）
- 【直前のやり取り】【返信先のメッセージ】は、【メッセージ本文】を理解するための参考情報。
- 抽出するのは【メッセージ本文】（添付画像があれば画像の内容も本文に含める）によって決まった・変わった・取り消された予定だけ。
  例: 直前に「来週の定例、15日と16日どっちがいい？」→ 本文「15日で決定です」なら、15日の定例を抽出する。
  例: 直前に「明日14時から打ち合わせでどうですか」→ 本文「了解です、それでお願いします」なら、明日14時の打ち合わせを抽出する。
- 本文が予定と関係ない、または直前のやり取りの中だけで完結している予定（本文で何も決まっていない）は抽出しない。

# 誰の予定か（audience）
- me: 【利用者】本人が参加・対応する予定。利用者とのDM、利用者の名前へのメンション・呼びかけ、利用者自身の発言など。
- all: グループ・チャンネル全員向けの予定。「皆さん」「全員」「各自」「@everyone」「@here」、【所属グループ】へのメンション、全体への連絡など。
- others: 利用者以外の特定の人だけの予定。「田中さんは15日に面談です」のように、利用者の名前が出てこない個人宛ての予定や、
  【所属グループ】以外のグループ・ロールだけに宛てた予定。
- unknown: どちらとも判断できない。迷ったら unknown にする（others にするのは、はっきり他の人だけの予定のときに限る）。

# 新しい予定か、登録済みの予定の変更・取り消しか（action）
- 【登録済みの予定】に、すでに登録されている今後の予定が「#番号 日付 時刻 タイトル」の形で載っている。
- add: 新しい予定（【登録済みの予定】にないもの）。
- same: 登録済みの予定と同じ予定を、改めて案内・リマインド・転送しているだけの連絡（日付も時刻も変わらない）。
  タイトルの言い方が違っても（「定例」「週次MTG」など）、同じ日時の同じ予定なら same にする。
  target_id にその予定の #番号を入れ、title〜description にはメッセージから読み取れる内容を書く。
- update: 登録済みの予定の日付・時刻・場所・内容が変わるという連絡。
  （「延期」「変更」「リスケ」「前倒し」「○日に変更」「開始を△時にずらす」「場所が××に変わった」など）
- cancel: 登録済みの予定がなくなるという連絡。（「中止」「キャンセル」「なくなった」「取りやめ」「休講」など）
- 「中止になるかも」「変更を検討中」など、まだ決まっていない話は抽出しない。
- update / cancel のとき:
  - target_id: 対象の予定の #番号（数字のみ）。【登録済みの予定】に見当たらなければ 0。
  - target_title / target_date: 対象の予定の元のタイトルと元の日付(YYYY-MM-DD)。分からなければ ""。
  - update の title〜description には「変更後」の内容を書く。変わらない項目は登録済みの予定の値をそのまま写す。
  - cancel の title〜description には取り消す予定の内容を書く。
- add のときは target_id を 0 にする。add / same のときは target_title と target_date を "" にする。

# 各項目
- title: 予定の内容を簡潔に（20文字以内目安）。例: 「定例ミーティング」「歯医者」「レポート提出締切」
  誰かと会う・遊ぶ予定は、分かれば相手の名前を入れる。例: 【送信者】田中 の「明日遊ぼう」→「田中と遊ぶ」
- date: YYYY-MM-DD。相対的な表現は【基準日時】から計算し、【参照カレンダー】で曜日を必ず確認する。
  年が省略されている場合は、基準日以降で最も近い日付にする。
- start_time / end_time: 24時間制 HH:MM。「午後3時」→15:00、「正午」「お昼」→12:00。
  時刻が明示されていない場合は空文字 ""。終了時刻が不明なら end_time は ""。
- location: 場所・会議URL（Zoom/Meet等）。なければ ""。
- description: 持ち物・補足など。なければ ""。
- confidence: これが実際に予定として登録すべきものである確信度（0〜1）。
  誘い・提案も、日付が分かれば決まった予定と同じように扱う（返事がないことを理由に低くしない）。
  update / cancel の場合は、その変更・取り消しが確定していて、対象の予定も合っている確信度。

# 画像
- 画像（スクリーンショット、チラシ、お知らせ、招待状、時間割など）が添付されている場合は、画像内の文字を読み取り、
  メッセージ本文の一部として扱う。本文がなく画像だけのときは、画像に書かれている予定を抽出する。
- 画像の中の日時・場所・持ち物なども、本文に書かれている場合と同じように各項目に入れる。

# 表（シフト表・当番表など、複数の人の予定が並ぶもの）
- 【利用者】の名前の欄だけを、1 日ずつ別々の予定として抽出する。利用者以外の人の分は抽出しない。
  表の中に利用者の名前がなければ、表からは何も抽出しない。表の中の店・教室の行事（発注・会議など）も、
  利用者の名前と一緒に書かれていなければ抽出しない。
- 本文の「【PDF「…」の内容】」は PDF の文字データで、カレンダー形式の表は「YYYY-MM-DD(曜): 欄の中身 / …」の形に
  日ごとに整理してある（日付はそのまま使う）。「★」の付いた欄が利用者の名前がある欄。
  ★ の欄の時刻を start_time / end_time にする（例: ★山田 16:00〜20:15 → 16:00 と 20:15）。
- シフトの title は「シフト」とし、表の見出し・ファイル名・表の外の文字から分かる店・教室などの名前を前に付ける
  （例: 「〇〇店 シフト」）。利用者の名前は title に入れない。location にもその店・教室の名前を入れる。

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


def format_context(context: Optional[list[ContextMessage]], tz) -> str:
    lines = []
    for c in context or []:
        t = c.sent_at.astimezone(tz)
        body = " ".join(c.text.split())[:300]
        lines.append(f"{t.month}/{t.day} {t.strftime('%H:%M')} {c.author or '（不明）'}: {body}")
    return "\n".join(lines)


# ---------- 日時の手がかり（「予定なし」と答えたときの聞き直し用） ----------
# 小さいローカルLLM は「13日16:30から理容室」「11/13 16:30 理容室」のような短いメモ書きを、
# 言い回しが少し違うだけで「予定なし」と答えることがある（2026-11 実機の qwen3-vl:8b-instruct）。
# 本文に日付と時刻がはっきり書かれているのに予定なしと答えたときは、プログラムで読み取った日時を添えて聞き直す。
_NUM_DATE = re.compile(r"(?<![\d/.])(?:(\d{4})[/年.-])?(\d{1,2})(?:/|月)(\d{1,2})日?(?![\d/])")
_DAY_ONLY = re.compile(r"(?<![\d/.月])(\d{1,2})日(?!間|前|後|ぶり|振り|目|分|ほど|くらい|ぐらい)")
_RELATIVE_DAYS = {"今日": 0, "本日": 0, "明日": 1, "あした": 1, "明後日": 2, "あさって": 2}
_RELATIVE = re.compile("|".join(sorted(_RELATIVE_DAYS, key=len, reverse=True)))
_TIME = re.compile(r"(午前|午後)?(\d{1,2})(?::(\d{2})|時(?![間限])(?:(半)|(\d{1,2})分)?)")
RECENT_PAST_DAYS = 90  # 年のない「10/30」が基準日の少し前なら、過去の話とみなす（来年の日付にしない）


def _valid_date(y: int, m: int, d: int) -> Optional[date]:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def _resolve_date(y: Optional[str], m: Optional[str], d: str, base: date) -> Optional[date]:
    """本文の日付を、基準日をもとに YYYY-MM-DD にする。過去の日付・ありえない日付は None。"""
    day = int(d)
    if m is None:  # 「13日」だけ → 今月、過ぎていれば来月（31日のない月は飛ばす）
        y0, m0 = base.year, base.month
        for _ in range(3):
            got = _valid_date(y0, m0, day)
            if got and got >= base:
                return got
            y0, m0 = (y0 + 1, 1) if m0 == 12 else (y0, m0 + 1)
        return None
    if y is not None:
        got = _valid_date(int(y), int(m), day)
        return got if got and got >= base else None
    got = _valid_date(base.year, int(m), day)
    if got is None:
        return None
    if got >= base:
        return got
    if (base - got).days <= RECENT_PAST_DAYS:
        return None
    return _valid_date(base.year + 1, int(m), day)


def datetime_hint(text: str, base: datetime) -> str:
    """本文にある今日以降の日付と時刻。両方そろっていなければ ""。"""
    t = unicodedata.normalize("NFKC", text)
    today = base.date()
    dates: list[str] = []
    taken: list[tuple[int, int]] = []
    for m in _NUM_DATE.finditer(t):
        taken.append(m.span())
        got = _resolve_date(m.group(1), m.group(2), m.group(3), today)
        if got:
            dates.append(f"「{m.group(0)}」= {got.isoformat()}({WEEKDAYS_JA[got.weekday()]})")
    for m in _DAY_ONLY.finditer(t):
        if any(s <= m.start() < e for s, e in taken):
            continue
        got = _resolve_date(None, None, m.group(1), today)
        if got:
            dates.append(f"「{m.group(0)}」= {got.isoformat()}({WEEKDAYS_JA[got.weekday()]})")
    for m in _RELATIVE.finditer(t):
        got = today + timedelta(days=_RELATIVE_DAYS[m.group(0)])
        dates.append(f"「{m.group(0)}」= {got.isoformat()}({WEEKDAYS_JA[got.weekday()]})")
    times: list[str] = []
    for m in _TIME.finditer(t):
        hour = int(m.group(2))
        minute = int(m.group(3) or m.group(5) or (30 if m.group(4) else 0))
        if m.group(1) == "午後" and hour < 12:
            hour += 12
        if hour > 24 or minute > 59:
            continue
        times.append(f"「{m.group(0)}」= {hour:02d}:{minute:02d}")
    dates, times = list(dict.fromkeys(dates))[:3], list(dict.fromkeys(times))[:3]
    if not dates or not times:
        return ""
    return (
        "本文に次の日付・時刻が書かれています（プログラムで読み取ったもの）: " + "、".join(dates + times) + "\n"
        "用事・予約・約束・締切などの予定なら、「5日10時 歯医者」のような短いメモ書きでも抽出してください。"
        "過去の出来事の報告・雑談・仮定の話・まだ決まっていない候補日なら、events は [] のままにしてください。"
    )


def build_user_prompt(
    text: str,
    sent_at: datetime,
    author: str = "",
    channel: str = "",
    image_count: int = 0,
    existing: Optional[list[Event]] = None,
    context: Optional[list[ContextMessage]] = None,
    reply_to: str = "",
    my_names: Optional[list[str]] = None,
    my_groups: Optional[list[str]] = None,
    hint: str = "",
) -> str:
    base = f"{sent_at.strftime('%Y-%m-%d')}({WEEKDAYS_JA[sent_at.weekday()]}) {sent_at.strftime('%H:%M')}"
    names = " / ".join(my_names or [])
    parts = [
        f"【利用者】{names}（この名前へのメンション「@名前」や呼びかけは利用者宛て）" if names else "【利用者】（名前は未設定）",
    ]
    if my_groups:
        parts.append(f"【所属グループ】{' / '.join(my_groups)}（利用者が所属するグループ・ロール。ここへのメンションは all）")
    parts += [
        f"【基準日時】{base}（メッセージ送信日時, {sent_at.tzname() or ''}）",
    ]
    if author:
        parts.append(f"【送信者】{author}")
    if channel:
        dm = "（利用者との1対1のメッセージ）" if channel.endswith("DM") else ""
        parts.append(f"【チャンネル】{channel}{dm}")
    if image_count:
        parts.append(f"【添付画像】{image_count}枚（画像内の文字もメッセージの一部）")
    parts.append("【参照カレンダー】\n" + build_calendar(sent_at))
    parts.append("【登録済みの予定】\n" + format_existing(existing))
    if context:
        parts.append("【直前のやり取り】（同じ場所の直前のメッセージ。古い順。参考情報）\n" + format_context(context, sent_at.tzinfo))
    if reply_to.strip():
        parts.append("【返信先のメッセージ】\n" + " ".join(reply_to.split())[:500])
    if text.strip():
        body = text.strip()
    elif image_count:
        body = "（本文なし。添付画像に書かれている内容を本文として読み取る）"
    else:
        body = "（本文なし）"
    if hint:
        parts.append("【日時の手がかり】\n" + hint)
    parts.append("【メッセージ本文】\n" + body)
    parts.append("上記から予定を抽出し、JSONで出力してください。")
    return "\n\n".join(parts)
