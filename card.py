"""Картинка графіка для каналу: 1080×1350, читається на телефоні без збільшення.

12 рядків-черг, на кожному доба як смуга: зелений трек — світло є,
червоні блоки — відключення з годинами всередині. Тривалість по чергах — у підписі до поста.
"""
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT = Path(__file__).parent / "fonts" / "Manrope.ttf"
QUEUES = [f"{i}.{j}" for i in range(1, 7) for j in (1, 2)]

W, H = 1080, 1350
PAD = 48
BG = "#F4F2EC"
CARD = "#FFFFFF"
INK = "#15171B"
MUTED = "#7A7E86"
FAINT = "#C9CCD1"
TRACK = "#DDEFE0"      # світло є
TICK = "#F4FAF5"
OFF = "#E5484D"        # світла немає (жорсткий режим)
OFF_TEXT = "#FFFFFF"
MAYBE = "#F2B23A"      # можливе відключення (легкий режим: графік є, але застосовують не завжди)
MAYBE_TEXT = "#3B2A06"   # темний текст на бурштиновому — контраст краще за білий
ACCENT = "#FFB800"
CHANGED = "#FFF1C2"

_fonts: dict[tuple[int, int], ImageFont.FreeTypeFont] = {}


def font(size: int, weight: int = 500) -> ImageFont.FreeTypeFont:
    key = (size, weight)
    if key not in _fonts:
        f = ImageFont.truetype(str(FONT), size)
        f.set_variation_by_axes([weight])
        _fonts[key] = f
    return _fonts[key]


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _time_parts(a: str, b: str) -> list[tuple[str, bool]]:
    """("16:30", "20:00") -> [("16", F), ("30", T), ("–20", F)]: хвилини йдуть верхнім індексом."""
    parts = []
    for i, hhmm in enumerate((a, b)):
        h, m = hhmm.split(":")
        parts.append((("–" if i else "") + str(int(h)), False))
        if m != "00":
            parts.append((m, True))
    return parts


def _range_width(d, parts, size) -> float:
    return sum(d.textlength(t, font=font(int(size * 0.62) if sup else size, 800)) for t, sup in parts)


def _draw_range(d, cx, cy, parts, size, fill):
    x = cx - _range_width(d, parts, size) / 2
    for t, sup in parts:
        f = font(int(size * 0.62) if sup else size, 800)
        d.text((x, cy - size * 0.28 if sup else cy), t, font=f, fill=fill, anchor="lm")
        x += d.textlength(t, font=f)


def _bolt(d: ImageDraw.ImageDraw, x: int, y: int, s: int):
    """Блискавка в жовтому колі (емодзі в шрифті немає)."""
    d.ellipse([x, y, x + s, y + s], fill=ACCENT)
    k = s / 24
    pts = [(13, 4), (6, 14), (11, 14), (10, 20), (18, 9), (13, 9), (14, 4)]
    d.polygon([(x + px * k, y + py * k) for px, py in pts], fill=INK)


def render(title: str, subtitle: str, queues: dict, *, updated: str | None = None,
           changed: set[str] = frozenset(), footer: str = "", light: bool = False) -> bytes:
    """light=True — легкий режим: бурштинові «можливі відключення» замість червоних."""
    off, off_text = (MAYBE, MAYBE_TEXT) if light else (OFF, OFF_TEXT)
    legend = "можливе відключення" if light else "немає світла"
    """title: «Завтра, 14 жовтня»; queues: {"1.1": [("08:00", "12:00")], ...}."""
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # --- шапка ---
    _bolt(d, PAD, 58, 34)
    d.text((PAD + 48, 75), "ЖИТОМИРЩИНА · ГРАФІК МОЖЛИВИХ ВІДКЛЮЧЕНЬ" if light else "ЖИТОМИРЩИНА · ГРАФІК ВІДКЛЮЧЕНЬ", font=font(26, 700), fill=MUTED, anchor="lm")
    if updated:
        label = f"ОНОВЛЕНО {updated}"
        f = font(24, 800)
        tw = d.textlength(label, font=f)
        d.rounded_rectangle([W - PAD - tw - 36, 54, W - PAD, 96], radius=21, fill=ACCENT)
        d.text((W - PAD - 18, 75), label, font=f, fill=INK, anchor="rm")
    d.text((PAD, 110), title, font=font(76, 800), fill=INK)
    d.text((PAD, 205), subtitle, font=font(32, 600), fill=MUTED)

    # --- картка з графіком ---
    top, left, right = 270, PAD, W - PAD
    label_w, inner = 72, 26
    x0, x1 = left + inner + label_w, right - inner - 8
    per_min = (x1 - x0) / 1440
    row_h, bar_h, pair_gap = 56, 42, 16
    rows_top = top + 78
    bottom = rows_top + 12 * row_h + 5 * pair_gap + 22
    d.rounded_rectangle([left, top, right, bottom], radius=32, fill=CARD)

    for h in range(0, 25, 3):  # вісь годин
        x = x0 + h * 60 * per_min
        d.text((x, top + 42), f"{h:02d}", font=font(24, 600), fill=MUTED, anchor="mm")
        d.line([x, rows_top - 6, x, bottom - 18], fill="#EEF0F2", width=2)

    y = rows_top
    for i, q in enumerate(QUEUES):
        cy = y + row_h / 2
        slots = queues.get(q) or []
        if q in changed:
            d.rounded_rectangle([left + 12, y + 4, right - 12, y + row_h - 4], radius=16, fill=CHANGED)
        d.text((left + inner, cy), q, font=font(34, 800), fill=INK, anchor="lm")

        # трек «світло є» з тонкими поділками щогодини
        d.rounded_rectangle([x0, cy - bar_h / 2, x1, cy + bar_h / 2], radius=12, fill=TRACK)
        for h in range(1, 24):
            x = x0 + h * 60 * per_min
            d.line([x, cy - bar_h / 2 + 8, x, cy + bar_h / 2 - 8], fill=TICK, width=2)

        for a, b in slots:
            ma, mb = minutes(a), minutes(b)
            bx0, bx1 = x0 + ma * per_min, x0 + mb * per_min
            d.rounded_rectangle([bx0, cy - bar_h / 2, bx1, cy + bar_h / 2], radius=12, fill=off)
            # Не влазить навіть дрібним — без тексту: точні години є в підписі до поста.
            parts = _time_parts(a, b)
            for size in (24, 22, 20):
                if _range_width(d, parts, size) + 8 <= bx1 - bx0:
                    _draw_range(d, (bx0 + bx1) / 2, cy + 1, parts, size, off_text)
                    break

        y += row_h + (pair_gap if i % 2 else 0)

    # --- легенда й підпис ---
    ly = bottom + 44
    d.rounded_rectangle([PAD, ly - 13, PAD + 40, ly + 13], radius=7, fill=off)
    d.text((PAD + 54, ly), legend, font=font(26, 600), fill=INK, anchor="lm")
    lx = PAD + 54 + d.textlength(legend, font=font(26, 600)) + 40
    d.rounded_rectangle([lx, ly - 13, lx + 40, ly + 13], radius=7, fill=TRACK)
    d.text((lx + 54, ly), "світло є", font=font(26, 600), fill=INK, anchor="lm")
    d.text((W - PAD, ly), "Дані: Житомиробленерго", font=font(24, 500), fill=MUTED, anchor="rm")

    if footer:
        fy = H - PAD - 76
        d.rounded_rectangle([PAD, fy, W - PAD, fy + 76], radius=38, fill=INK)
        d.text((W / 2, fy + 38), footer, font=font(30, 700), fill="#FFFFFF", anchor="mm")

    buf = BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
