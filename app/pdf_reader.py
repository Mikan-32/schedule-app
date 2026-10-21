"""貼り付けた PDF から、ローカルLLM に渡す文字（と画像）を取り出す。

Excel などから作った PDF には文字データが入っているので、スクリーンショットを読ませるより正確に読める
（シフト表のスクリーンショットでは、ある人の 10 日分のうち 5 日しか正しく読めず、ない日まで作った）。

- 背景と同じ色の文字（Excel で 0:00 などを白い文字にして隠しているもの）は除く
- カレンダー形式の表（シフト表・月の予定表）は、日ごとに「2026-11-04(水): 欄の中身 / …」の形に整理する
  （LLM に表の列と日付を対応させなくて済むように）
- 利用者の名前が出てくる欄には「★」を付ける（シフト表から自分の分だけを抜き出しやすくする）
- 文字データがほとんどないページ（スキャンした PDF など）は画像にして渡す
"""
from __future__ import annotations

import calendar
import io
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from typing import Iterable, Optional

from .audience import find_term
from .formatting import WEEKDAYS_JA
from .models import ImageInput

MAX_PAGES = 10  # 読むページ数
MAX_TEXT_CHARS = 6000  # LLM に渡す文字数（num_ctx=8192 に収まるように）
MIN_PAGE_CHARS = 20  # これより文字が少ないページは、文字データがないとみなして画像にする
MAX_RENDERED_PAGES = 4
RENDER_LONG_SIDE = 1600
PDF_MARK = "【PDF「"  # 貼り付けた内容に PDF が入っているかの目印

_DAY = re.compile(r"^(\d{1,2})日?(?:\s+|$)")
_TIME_RANGE = re.compile(r"(?<![\d:])(\d{1,2}:\d{2})\s*(?:[-~〜～－]\s*)?(\d{1,2}:\d{2})(?![\d:])")
_YEAR_MONTH = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月")
_YEAR_MONTH_LOOSE = re.compile(r"(?<!\d)(20\d{2})\s+(\d{1,2})(?!\d)")
_WEEKDAY_CELLS = {**{w: i for i, w in enumerate(WEEKDAYS_JA)},
                  **{w: i for i, w in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))}}


class PdfError(ValueError):
    """PDF を開けない（壊れている・パスワード付きなど）。"""


class PdfUnavailable(RuntimeError):
    """PDF を読むライブラリ（pdfplumber）が入っていない。"""


@dataclass
class PdfContent:
    text: str = ""
    images: list[ImageInput] = field(default_factory=list)
    pages: int = 0  # PDF の全ページ数


def _libs():
    try:
        import pdfplumber
        import pypdfium2
    except ImportError as e:
        raise PdfUnavailable(
            "PDF を読むライブラリが入っていません。アプリのフォルダで "
            "`.venv\\Scripts\\python.exe -m pip install -r requirements.txt`（Mac・Linux は .venv/bin/python）を実行して、"
            "アプリを再起動してください"
        ) from e
    return pdfplumber, pypdfium2


# ---------- 見えない文字を除く ----------
def _rgb(color) -> Optional[tuple[float, float, float]]:
    """pdfplumber の色（グレー / RGB / CMYK）を RGB にする。パターンなど分からないものは None。"""
    if color is None:
        return None
    if isinstance(color, (int, float)):
        color = (color,)
    try:
        vals = tuple(float(v) for v in color)
    except (TypeError, ValueError):
        return None
    if len(vals) == 1:
        return (vals[0],) * 3
    if len(vals) == 3:
        return vals  # type: ignore[return-value]
    if len(vals) == 4:
        c, m, y, k = vals
        return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    return None


def visible_page(page):
    """背景（その下に塗られた四角形。なければ白）とほぼ同じ色の文字を除いたページ。"""
    fills = [
        (r["x0"], r["top"], r["x1"], r["bottom"], color)
        for r in page.rects
        if r.get("fill") and (color := _rgb(r.get("non_stroking_color"))) is not None
    ]

    def keep(obj) -> bool:
        if obj.get("object_type") != "char":
            return True
        color = _rgb(obj.get("non_stroking_color"))
        if color is None:
            return True
        x = (obj["x0"] + obj["x1"]) / 2
        y = (obj["top"] + obj["bottom"]) / 2
        bg = (1.0, 1.0, 1.0)
        for x0, top, x1, bottom, fill in fills:  # 後から描かれた塗りが上
            if x0 <= x <= x1 and top <= y <= bottom:
                bg = fill
        return max(abs(a - b) for a, b in zip(color, bg)) > 0.08

    return page.filter(keep)


# ---------- カレンダー形式の表 ----------
def _clean(cell) -> str:
    return (cell or "").strip()


def _day_of(cell: str) -> Optional[tuple[int, str]]:
    """欄の先頭の日付（1〜31）と、その後ろの文字（「4 PG」→ (4, "PG")）。時刻（9:30）は日付ではない。"""
    m = _DAY.match(cell)
    if not m:
        return None
    day = int(m.group(1))
    if not 1 <= day <= 31:
        return None
    return day, cell[m.end():].strip()


def _weekday_header(row: list) -> Optional[dict[int, int]]:
    """「月 火 水 木 金 土 日」の見出しの行なら {列: 曜日(月=0)}。"""
    cols = {}
    for c, cell in enumerate(row):
        key = _clean(cell).lower()[:3]
        if key in _WEEKDAY_CELLS:
            cols[c] = _WEEKDAY_CELLS[key]
    return cols if len(cols) >= 5 else None


def _format_entry(text: str, names: Iterable[str]) -> str:
    text = " ".join(text.split())
    text = _TIME_RANGE.sub(r"\1〜\2", text)
    return f"★{text}" if find_term(text, names) else text


@dataclass
class _Calendar:
    days: list[tuple[int, int, list[str]]]  # (月のずれ, 日, 欄の中身)
    loose: list[str]  # どの日か分からない欄
    weekday_of_col: dict[int, int]  # 見出しの曜日（列 → 曜日）
    day_cols: list[int]
    seen: list[tuple[int, int, int]]  # (月のずれ, 日, 列) 曜日の確かめ用


def _parse_calendar(rows: list[list]) -> Optional[_Calendar]:
    """カレンダー形式の表なら、日ごとの中身にまとめる。違えば None。"""
    # 日付が 2 つ以上並ぶ行から、日付の列を決める
    col_hits: dict[int, int] = {}
    for row in rows:
        hits = [c for c, cell in enumerate(row) if _day_of(_clean(cell))]
        if len(hits) >= 2:
            for c in hits:
                col_hits[c] = col_hits.get(c, 0) + 1
    day_cols = sorted(col_hits)
    if len(day_cols) < 5:
        return None

    weekday_of_col: dict[int, int] = {}
    days: dict[tuple[int, int], list[str]] = {}
    order: list[tuple[int, int]] = []
    loose: list[str] = []
    seen: list[tuple[int, int, int]] = []
    week: dict[int, tuple[int, int]] = {}  # 列 → (月のずれ, 日)
    offset, last_day = 0, 0
    for row in rows:
        if header := _weekday_header(row):
            weekday_of_col.update(header)
            continue
        cells = [(c, _clean(cell)) for c, cell in enumerate(row) if _clean(cell)]
        day_cells = {c: d for c, cell in cells if c in col_hits and (d := _day_of(cell))}
        if day_cells:  # 日付の行（新しい週）
            week = {}
            for c in sorted(day_cells):
                day = day_cells[c][0]
                if last_day and day < last_day - 7:
                    offset += 1  # 31 → 1 のように月が変わった
                last_day = day
                week[c] = (offset, day)
                seen.append((offset, day, c))
                if (offset, day) not in days:
                    days[(offset, day)] = []
                    order.append((offset, day))
        for c, cell in cells:
            text = day_cells[c][1] if c in day_cells else cell
            if not text:
                continue
            base = max((d for d in day_cols if d <= c), default=None)
            key = week.get(base) if base is not None else None
            lines = [ln for ln in text.split("\n") if ln.strip()]
            if key is None:
                loose.extend(lines)
            else:
                days[key].extend(lines)
    if len(order) < 20:
        return None
    # 最初の週に前の月の日付（28 29 30 31 1 …）があれば、それを前の月にする
    if order and order[0][0] == 0 and any(o == 1 for o, _ in order[:8]) and order[0][1] >= 22:
        first_wrap = next(i for i, (o, _) in enumerate(order) if o == 1)
        if first_wrap <= 7:
            shift = {k: (k[0] - 1, k[1]) for k in order}
            days = {shift[k]: v for k, v in days.items()}
            seen = [(o - 1, d, c) for o, d, c in seen]
            order = [shift[k] for k in order]
    return _Calendar([(o, d, days[(o, d)]) for o, d in order], loose, weekday_of_col, day_cols, seen)


def _year_month(texts: Iterable[str]) -> Optional[tuple[int, int]]:
    texts = [unicodedata.normalize("NFKC", t or "") for t in texts]
    for t in texts:
        if m := _YEAR_MONTH.search(t):
            if 1 <= int(m.group(2)) <= 12:
                return int(m.group(1)), int(m.group(2))
    for t in texts:  # 「2026」「11」「年」「月」が別々の欄にある Excel の表
        if "年" in t and "月" in t and (m := _YEAR_MONTH_LOOSE.search(t)):
            if 1 <= int(m.group(2)) <= 12:
                return int(m.group(1)), int(m.group(2))
    return None


def _add_months(year: int, month: int, n: int) -> tuple[int, int]:
    m = month - 1 + n
    return year + m // 12, m % 12 + 1


def _date_labels(cal: _Calendar, ym: Optional[tuple[int, int]]) -> Optional[dict[tuple[int, int], date]]:
    """年月が分かれば、日付に直す。見出しの曜日と合わない（年月の読み違い）なら None。"""
    if ym is None:
        return None
    out: dict[tuple[int, int], date] = {}
    for o, d, _ in cal.days:
        y, m = _add_months(*ym, o)
        if d > calendar.monthrange(y, m)[1]:
            return None
        out[(o, d)] = date(y, m, d)
    if cal.weekday_of_col:
        checks = [(out[(o, d)].weekday(), cal.weekday_of_col.get(c)) for o, d, c in cal.seen if c in cal.weekday_of_col]
        if checks and sum(a == b for a, b in checks) < len(checks) * 0.8:
            return None
    return out


def _format_calendar(cal: _Calendar, ym: Optional[tuple[int, int]], names: list[str]) -> list[str]:
    dates = _date_labels(cal, ym)
    lines = []
    for o, d, entries in cal.days:
        if not entries:
            continue
        if dates:
            dt = dates[(o, d)]
            label = f"{dt.isoformat()}({WEEKDAYS_JA[dt.weekday()]})"
        else:
            label = f"{'翌月' if o > 0 else '前月' if o < 0 else ''}{d}日"
        lines.append(f"{label}: " + " / ".join(_format_entry(e, names) for e in entries))
    return lines


def _format_table(rows: list[list], names: list[str]) -> list[str]:
    lines = []
    for row in rows:
        cells = [" ".join(_clean(c).split()) for c in row if _clean(c)]
        if cells:
            lines.append(_format_entry(" | ".join(cells), names))
    return lines


# ---------- PDF 全体 ----------
def read_pdf(data: bytes, filename: str = "", names: Iterable[str] = ()) -> PdfContent:
    """PDF から LLM に渡す文字（と、文字データのないページの画像）を作る。"""
    pdfplumber, pdfium = _libs()
    names = [n for n in names if n]
    out = PdfContent()
    parts: list[str] = []
    render_pages: list[int] = []
    try:
        pdf = pdfplumber.open(io.BytesIO(data))
    except Exception as e:
        raise PdfError(f"PDF を開けませんでした（壊れている・パスワード付きなど）: {e}") from e
    with pdf:
        out.pages = len(pdf.pages)
        for i, page in enumerate(pdf.pages[:MAX_PAGES]):
            page = visible_page(page)
            page_text = page.extract_text() or ""
            if len(re.sub(r"\s", "", page_text)) < MIN_PAGE_CHARS:
                render_pages.append(i)
                continue
            where = f"{i + 1}ページ目" if out.pages > 1 else ""
            tables = page.find_tables()
            ym = _year_month([filename, page_text])
            outside = page
            for t in tables:
                rows = t.extract()
                cal = _parse_calendar(rows)
                if cal:
                    star = "。★ は利用者の名前がある欄" if names else ""
                    parts.append(f"■ {where}の表（カレンダー。日ごとに整理{star}）".replace("■ の", "■ "))
                    parts.extend(_format_calendar(cal, ym, names))
                    if cal.loose:
                        parts.append("（日付の分からない欄）" + " / ".join(cal.loose))
                else:
                    parts.append(f"■ {where}の表".replace("■ の", "■ "))
                    parts.extend(_format_table(rows, names))
                outside = outside.outside_bbox(t.bbox)
            rest = (outside.extract_text() or "").strip() if tables else page_text.strip()
            if rest:
                parts.append(f"■ {where}{'の表の外の文字' if tables else 'の文字'}".replace("■ の", "■ "))
                parts.extend(_format_entry(ln, names) for ln in rest.split("\n") if ln.strip())
    text = "\n".join(parts)
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + "\n（長いので以降は省略）"
    if out.pages > MAX_PAGES:
        text += f"\n（{MAX_PAGES + 1}ページ目以降は省略）"
    if text.strip():
        out.text = f"{PDF_MARK}{filename or '（名前なし）'}」の内容（文字データから読み取り）】\n{text}"
    if render_pages:
        out.images = _render(pdfium, data, render_pages[:MAX_RENDERED_PAGES], filename)
    return out


def _render(pdfium, data: bytes, pages: list[int], filename: str) -> list[ImageInput]:
    images = []
    doc = pdfium.PdfDocument(data)
    try:
        for i in pages:
            page = doc[i]
            w, h = page.get_size()
            bitmap = page.render(scale=RENDER_LONG_SIDE / max(w, h, 1))
            buf = io.BytesIO()
            bitmap.to_pil().convert("RGB").save(buf, format="PNG")
            images.append(ImageInput(buf.getvalue(), "image/png", f"{filename or 'pdf'} p{i + 1}"))
    finally:
        doc.close()
    return images
