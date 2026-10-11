"""日本の祝日（「国民の祝日に関する法律」）を計算する。

- 対象は 2020〜2099 年（天皇誕生日が 2 月 23 日になった年から、春分・秋分の近似式が使える年まで）。範囲外は祝日なし
- 春分の日・秋分の日は前年 2 月に官報で正式に決まるが、近似式で計算する（1980〜2099 年は公表値と一致）
- 振替休日（祝日が日曜 → その後の最も近い祝日でない日）と国民の休日（祝日にはさまれた日）も含める
- 祝日法が変わったら、ここと tests/test_holidays.py を更新する（内閣府「国民の祝日について」に翌年分が毎年 2 月に載る）
"""
from __future__ import annotations

from datetime import date, timedelta
from functools import lru_cache

MIN_YEAR, MAX_YEAR = 2020, 2099

# 東京オリンピック・パラリンピックの特例で移動した祝日
_MOVED = {
    2020: {"海の日": date(2020, 7, 23), "スポーツの日": date(2020, 7, 24), "山の日": date(2020, 8, 10)},
    2021: {"海の日": date(2021, 7, 22), "スポーツの日": date(2021, 7, 23), "山の日": date(2021, 8, 8)},
}


def _nth_monday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(7 - first.weekday()) % 7 + 7 * (n - 1))


def _equinox(year: int, base: float) -> int:
    y = year - 1980
    return int(base + 0.242194 * y) - y // 4


@lru_cache(maxsize=None)
def holidays_in_year(year: int) -> dict[date, str]:
    """その年の祝日・休日 {日付: 名称}。"""
    if not MIN_YEAR <= year <= MAX_YEAR:
        return {}
    national = {
        date(year, 1, 1): "元日",
        _nth_monday(year, 1, 2): "成人の日",
        date(year, 2, 11): "建国記念の日",
        date(year, 2, 23): "天皇誕生日",
        date(year, 3, _equinox(year, 20.8431)): "春分の日",
        date(year, 4, 29): "昭和の日",
        date(year, 5, 3): "憲法記念日",
        date(year, 5, 4): "みどりの日",
        date(year, 5, 5): "こどもの日",
        _nth_monday(year, 7, 3): "海の日",
        date(year, 8, 11): "山の日",
        _nth_monday(year, 9, 3): "敬老の日",
        date(year, 9, _equinox(year, 23.2488)): "秋分の日",
        _nth_monday(year, 10, 2): "スポーツの日",
        date(year, 11, 3): "文化の日",
        date(year, 11, 23): "勤労感謝の日",
    }
    for name, moved in _MOVED.get(year, {}).items():
        national = {d: n for d, n in national.items() if n != name}
        national[moved] = name

    result = dict(national)
    one = timedelta(days=1)
    for d in national:
        # 国民の休日: 前日と翌日が祝日で、自身は祝日でも日曜でもない日
        mid = d + one
        if mid not in national and mid + one in national and mid.weekday() != 6:
            result[mid] = "国民の休日"
        # 振替休日: 祝日が日曜なら、その後の最も近い祝日でない日
        if d.weekday() == 6:
            sub = d + one
            while sub in national:
                sub += one
            result[sub] = "振替休日"
    return dict(sorted(result.items()))


def holidays_between(start: date, end: date) -> list[tuple[date, str]]:
    """start〜end（両端を含む）の祝日・休日を日付順に返す。"""
    out: list[tuple[date, str]] = []
    for year in range(start.year, end.year + 1):
        out.extend((d, n) for d, n in holidays_in_year(year).items() if start <= d <= end)
    return out
