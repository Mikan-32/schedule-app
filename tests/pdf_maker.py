"""テスト用の小さな PDF を作る（英数字だけ。Helvetica）。座標は上からの距離で指定する。"""
from __future__ import annotations

W, H = 842, 595  # A4 横


def _y(top: float, height: float = 0) -> float:
    return H - top - height


class Page:
    def __init__(self):
        self.ops: list[str] = []

    def fill(self, x, top, w, h, color=(1, 1, 1)):
        self.ops.append(f"{color[0]} {color[1]} {color[2]} rg {x} {_y(top, h)} {w} {h} re f")

    def box(self, x, top, w, h):
        self.ops.append(f"0 0 0 RG 0.5 w {x} {_y(top, h)} {w} {h} re S")

    def text(self, x, top, s, color=(0, 0, 0), size=9):
        s = s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        self.ops.append(f"{color[0]} {color[1]} {color[2]} rg BT /F1 {size} Tf {x} {_y(top, size)} Td ({s}) Tj ET")


def make_pdf(pages: list[Page]) -> bytes:
    n = len(pages)
    objs: list[bytes] = [b"", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for p in pages:
        content = "\n".join(p.ops).encode("latin-1")
        objs.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {W} {H}] /Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {len(objs)} 0 R >>".encode()
        )
        kids.append(f"{len(objs)} 0 R")
    objs[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {n} >>".encode()
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


GRAY = (0.75, 0.75, 0.75)


def shift_calendar(entries: dict[int, list[str]], labels: dict[int, str] | None = None,
                   hidden: dict[int, list[str]] | None = None, first_weekday: int = 6, days: int = 30) -> bytes:
    """月曜始まりのカレンダー形式のシフト表（2026 年 11 月なら first_weekday=6: 1 日が日曜）。

    entries: 日 → 欄の行（見える文字）。hidden: 日 → 背景と同じ色で隠した行（Excel の 0:00 など）。
    日付の行は灰色の塗りで、日付のない欄には灰色の文字（見えない）で 0:00 を書いておく。
    """
    labels, hidden = labels or {}, hidden or {}
    p = Page()
    x0, top0, cw, dh, ch = 20, 20, 110, 14, 60
    for i, w in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")):
        p.box(x0 + i * cw, top0, cw, dh)
        p.text(x0 + i * cw + 45, top0 + 3, w)
    top = top0 + dh
    cells = [None] * first_weekday + list(range(1, days + 1))
    for week in range(0, len(cells), 7):
        row = cells[week:week + 7]
        for i in range(7):
            day = row[i] if i < len(row) else None
            x = x0 + i * cw
            p.fill(x, top, cw, dh, GRAY)
            p.box(x, top, cw, dh)
            if day:
                p.text(x + 3, top + 3, str(day))
                if day in labels:
                    p.text(x + 30, top + 3, labels[day], color=(1, 0, 0))
            else:
                p.text(x + 3, top + 3, "0:00", color=GRAY)  # 灰色の上の灰色（見えない）
            p.box(x, top + dh, cw, ch)
            lines = [(t, (0, 0, 0)) for t in entries.get(day, [])] + [(t, (1, 1, 1)) for t in hidden.get(day, [])]
            for j, (t, color) in enumerate(lines):
                p.text(x + 3, top + dh + 3 + j * 11, t, color=color)
        top += dh + ch
    p.text(x0 + 7 * cw + 20, top0, "2026")
    p.text(x0 + 7 * cw + 60, top0, "11")
    return make_pdf([p])
