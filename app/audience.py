"""登録の基準: 誰宛ての予定かを決め、宛先ごとの扱い（登録 / 確認待ち / 見送り）を選ぶ。

宛先は 4 つ。
  me      自分宛て       自分の名前が出てくる・自分の発言・DM・自分で貼り付けたメッセージ
  all     全員・グループ宛て  @everyone・@here・所属グループへのメンション、「皆さん」など全体への連絡
  unknown 誰宛てか分からない  名前も所属グループも出てこない
  others  他の人宛て      はっきり他の人（他のグループ）だけの予定

LLM の判断（audience）に、名前・DM・所属グループなどの決まったルールを組み合わせる。
- 自分の名前が出てくれば、LLM の判断にかかわらず「自分宛て」
- LLM が「自分宛て」と判断しても、名前・DM などの根拠がなければ「分からない」として扱う
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .models import ContextMessage, IncomingMessage

AUDIENCES = ("me", "all", "unknown", "others")
ACTIONS = ("add", "confirm", "skip")
AUDIENCE_LABELS = {"me": "自分宛て", "all": "全員・グループ宛て", "unknown": "誰宛てか分からない", "others": "他の人宛て"}
DEFAULT_ACTIONS = {"me": "add", "all": "confirm", "unknown": "confirm", "others": "skip"}
STATE_KEY = "audience_rules"  # Web UI で保存した基準（app_state）
MAX_ITEMS = 30
MAX_LEN = 40

_INVISIBLE = re.compile("[​-‏‪-‮⁦-⁩﻿]")
_EVERYONE = re.compile(r"@(everyone|here|channel|all|全員)(?![0-9a-z_])")
_ASCII_WORD = re.compile(r"^[0-9a-z_.\-]+$")


def normalize(text: str) -> str:
    """見えない文字を除き、全角・半角と大文字・小文字の違いをなくす。"""
    return unicodedata.normalize("NFKC", _INVISIBLE.sub("", text or "")).lower()


def find_term(text: str, terms: Iterable[str]) -> Optional[str]:
    """text に出てくる最初の語（設定の表記のまま）を返す。

    英数字だけの語は、単語の途中では一致させない（「hal」が「shall」に一致しない）。
    """
    hay = normalize(text)
    if not hay:
        return None
    for term in terms:
        t = normalize(term).strip().lstrip("@").strip()
        if not t:
            continue
        if _ASCII_WORD.match(t):
            if re.search(rf"(?<![0-9a-z_]){re.escape(t)}(?![0-9a-z_])", hay):
                return term
        elif t in hay:
            return term
    return None


def is_dm(channel: str) -> bool:
    """1 対 1 のメッセージか（Discord Bot は「DM」、Windows 通知は「Discord DM」）。"""
    return (channel or "").strip().endswith("DM")


def _is_me(author: str, names: Iterable[str]) -> bool:
    """送信者が自分か（表示名が名前と一致する。「名前 (ニックネーム)」の形も可）。"""
    a = normalize(author).strip()
    if not a:
        return False
    head = a.split(" (", 1)[0].strip()
    return any((n := normalize(name).strip().lstrip("@")) and n in (a, head) for name in names)


def clean_list(values) -> list[str]:
    """名前・グループの一覧を整える（前後の空白・先頭の @ を除き、重複をなくす）。"""
    if isinstance(values, str):
        values = re.split(r"[,、，\n]", values)
    out: list[str] = []
    seen: set[str] = set()
    for v in values or []:
        v = _INVISIBLE.sub("", str(v)).strip().lstrip("@＠").strip()[:MAX_LEN]
        key = normalize(v)
        if v and key not in seen:
            seen.add(key)
            out.append(v)
    return out[:MAX_ITEMS]


def clean_action(value, default: str) -> str:
    v = str(value or "").strip().lower()
    v = {"register": "add", "auto": "add", "hold": "confirm", "ask": "confirm", "pending": "confirm", "ignore": "skip"}.get(v, v)
    return v if v in ACTIONS else default


@dataclass
class AudienceRules:
    names: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    actions: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_ACTIONS))

    @classmethod
    def from_settings(cls, s) -> "AudienceRules":
        return cls(
            names=clean_list(s.my_names),
            groups=clean_list(s.my_groups),
            actions={
                "me": clean_action(s.audience_me, DEFAULT_ACTIONS["me"]),
                "all": clean_action(s.audience_all, DEFAULT_ACTIONS["all"]),
                "unknown": clean_action(s.audience_unknown, DEFAULT_ACTIONS["unknown"]),
                "others": clean_action(s.others_mode, DEFAULT_ACTIONS["others"]),
            },
        )

    @classmethod
    def from_dict(cls, data: dict, base: "AudienceRules") -> "AudienceRules":
        """保存された値・画面から送られた値。足りない項目は base のまま。"""
        actions = dict(base.actions)
        for key, value in (data.get("actions") or {}).items():
            if key in actions:
                actions[key] = clean_action(value, actions[key])
        return cls(
            names=clean_list(data["names"]) if "names" in data else list(base.names),
            groups=clean_list(data["groups"]) if "groups" in data else list(base.groups),
            actions=actions,
        )

    def apply_to(self, s) -> None:
        """settings に反映する（LLM に渡す【利用者】もこれで変わる）。"""
        s.my_names = list(self.names)
        s.my_groups = list(self.groups)
        s.audience_me = self.actions["me"]
        s.audience_all = self.actions["all"]
        s.audience_unknown = self.actions["unknown"]
        s.others_mode = self.actions["others"]

    def action_for(self, audience: str) -> str:
        return self.actions.get(audience, "add")

    def to_dict(self) -> dict:
        return {"names": list(self.names), "groups": list(self.groups), "actions": dict(self.actions)}


def load_saved(db, settings) -> None:
    """Web UI で保存した基準があれば settings に反映する（起動時。.env の値より優先）。"""
    raw = db.get_state(STATE_KEY)
    if not raw:
        return
    try:
        data = json.loads(raw)
    except ValueError:
        return
    if isinstance(data, dict):
        AudienceRules.from_dict(data, AudienceRules.from_settings(settings)).apply_to(settings)


def save(db, settings, data: dict) -> AudienceRules:
    rules = AudienceRules.from_dict(data, AudienceRules.from_settings(settings))
    db.set_state(STATE_KEY, json.dumps(rules.to_dict(), ensure_ascii=False))
    rules.apply_to(settings)
    return rules


@dataclass
class Judgement:
    audience: str  # me / all / unknown / others
    reason: str  # 根拠（確認待ち・通知に表示する）

    @property
    def label(self) -> str:
        return AUDIENCE_LABELS.get(self.audience, self.audience)


def judge(
    llm_audience: str,
    msg: IncomingMessage,
    rules: AudienceRules,
    context: Iterable[ContextMessage] = (),
) -> Judgement:
    """メッセージの予定が誰宛てかを決める。上から順に、最初に当てはまったものになる。"""
    names, groups = rules.names, rules.groups
    # 1. 自分の名前が出てくる・自分の発言 → 自分宛て（LLM が「他の人」と言っても）
    if name := find_term(msg.text, names):
        return Judgement("me", f"名前「{name}」が出てくる")
    if _is_me(msg.author, names):
        return Judgement("me", "自分の発言")
    # 2. LLM がはっきり他の人だけの予定と判断 → 他の人宛て
    if llm_audience == "others":
        return Judgement("others", "他の人だけの予定")
    # 3. DM・自分で貼り付けたメッセージ → 自分宛て
    if is_dm(msg.channel):
        return Judgement("me", "DM")
    if msg.source == "manual":
        return Judgement("me", "自分で貼り付けたメッセージ")
    if msg.reply_to and _is_me(msg.reply_to.split(": ", 1)[0], names):
        return Judgement("me", "自分のメッセージへの返信")
    # 4. 所属グループ・@everyone・@here → 全員・グループ宛て
    if group := find_term(f"{msg.text}\n{msg.channel}", groups):
        return Judgement("all", f"所属グループ「{group}」宛て")
    if m := _EVERYONE.search(normalize(msg.text)):
        return Judgement("all", f"{m.group(0)} 宛て")
    if llm_audience == "all":
        return Judgement("all", "全体への連絡")
    # 5. LLM が自分宛てと判断し、返信先・直前のやり取りに自分の名前がある → 自分宛て
    if llm_audience == "me":
        if find_term(msg.reply_to, names) or any(find_term(c.text, names) or _is_me(c.author, names) for c in context):
            return Judgement("me", "直前のやり取りに名前が出てくる")
        return Judgement("unknown", "名前や DM などの根拠がない")
    return Judgement("unknown", "名前も所属グループも出てこない")
