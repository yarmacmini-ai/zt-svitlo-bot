"""Тексти постів і розрахунок моментів перемикання. Без Telegram — лише дані й рядки.

Час усюди в хвилинах від початку «сьогодні» за Києвом: завтрашні 08:00 — це 1440 + 480.
"""
from datetime import date, timedelta

QUEUES = [f"{i}.{j}" for i in range(1, 7) for j in (1, 2)]
MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня",
          "серпня", "вересня", "жовтня", "листопада", "грудня"]
WEEKDAYS = ["понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя"]
WD_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "нд"]

Slots = list[tuple[str, str]]


# ---------- дрібниці ----------
def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def hhmm(total: int) -> str:
    total %= 1440
    return f"{total // 60:02d}:{total % 60:02d}"


def ranges(slots: Slots) -> str:
    return ", ".join(f"{a}–{b}" for a, b in slots)


def off_minutes(slots: Slots) -> int:
    return sum(minutes(b) - minutes(a) for a, b in slots)


def hours(total: int) -> str:
    return f"{total / 60:g} год".replace(".", ",")


def day_name(day: date, today: date) -> str:
    """«Сьогодні» / «Завтра» / «14 жовтня»."""
    return {0: "Сьогодні", 1: "Завтра"}.get((day - today).days, "")


def day_title(day: date, today: date) -> str:
    """Для картинки: «Завтра, 14 жовтня»."""
    name = day_name(day, today)
    base = f"{day.day} {MONTHS[day.month - 1]}"
    return f"{name}, {base}" if name else base.capitalize()


def day_inline(day: date, today: date) -> str:
    """Для тексту: «на завтра · 14 жовтня, вт»."""
    name = {0: "на сьогодні", 1: "на завтра"}.get((day - today).days, "на")
    return f"{name} · {day.day} {MONTHS[day.month - 1]}, {WD_SHORT[day.weekday()]}"


def summary(queues: dict[str, Slots]) -> str:
    off = [off_minutes(queues.get(q) or []) for q in QUEUES]
    hit = [m for m in off if m]
    if not hit:
        return "відключень не заплановано"
    span = hours(max(hit)) if min(hit) == max(hit) else f"від {hours(min(hit))[:-4]} до {hours(max(hit))}"
    return f"без світла {len(hit)} з 12 черг · {span}"


def card_subtitle(day: date, queues: dict[str, Slots]) -> str:
    return f"{WEEKDAYS[day.weekday()]} · {summary(queues)}"


def hour_bar(slots: Slots) -> str:
    """Дві смужки по 12 годин: 🟩 світло є, 🟥 немає, 🟧 пів години без світла."""
    off = [0] * 48
    for a, b in slots:
        for i in range(minutes(a) // 30, minutes(b) // 30):
            off[i] = 1
    cells = ["🟥" if off[2 * h] and off[2 * h + 1] else "🟧" if off[2 * h] or off[2 * h + 1] else "🟩"
             for h in range(24)]
    return f"<code>00</code> {''.join(cells[:12])}\n<code>12</code> {''.join(cells[12:])}"


# ---------- пост у канал ----------
def short(hhmm_: str) -> str:
    """"08:00" -> "8", "16:30" -> "16:30" — як на картинці."""
    h, m = hhmm_.split(":")
    h = "00" if h == "00" else str(int(h))  # «0–0:30» читається гірше за «00–00:30»
    return h if m == "00" else f"{h}:{m}"


def short_ranges(slots: Slots) -> str:
    return ", ".join(f"{short(a)}–{short(b)}" for a, b in slots)


def command_line(command: list[tuple[str, str, str]] | None) -> str | None:
    """«⚙️ Укренерго: 6–8 — 0,5 черги · 8–16 — 1,5 · …»; без обмежень — пропускаємо."""
    segs = [(a, b, label) for a, b, label in command or [] if "без" not in label]
    if not segs:
        return None
    parts = [f"{short(a)}–{short(b)} — {label if i == 0 else label.split()[0]}"
             for i, (a, b, label) in enumerate(segs)]
    return "⚙️ Укренерго: " + " · ".join(parts)


def grouped(queues: dict[str, Slots]) -> list[tuple[list[str], Slots]]:
    """Черги з однаковим графіком в один рядок, у порядку першої черги."""
    groups: dict[tuple, list[str]] = {}
    for q in QUEUES:
        groups.setdefault(tuple(queues.get(q) or []), []).append(q)
    return [(qs, list(slots)) for slots, qs in groups.items()]


def channel_caption(day: date, today: date, queues: dict[str, Slots], bot_username: str,
                    changed: set[str] | None = None, cancelled: bool = False,
                    command: list | None = None) -> str:
    """Підпис до картинки: шапка на 2–3 рядки, перелік черг — у згорнутій цитаті."""
    when = day_inline(day, today)
    if cancelled:
        head = f"✅ <b>Відключення {when} скасовано</b>"
    elif changed:
        head = f"🔄 <b>Оновлено графік {when}</b> · змінилось: {', '.join(sorted(changed))}"
    else:
        head = f"⚡️ <b>Графік відключень {when}</b>"
    lines = [head, summary(queues).capitalize()]
    cmd = command_line(command)
    if cmd and not cancelled:
        lines.append(cmd)
    rows = []
    for qs, slots in grouped(queues):
        mark = " ✏️" if changed and changed & set(qs) else ""
        body = f"{short_ranges(slots)} · {hours(off_minutes(slots))}" if slots else "світло є"
        rows.append(f"{'🔴' if slots else '🟢'} <b>{' · '.join(qs)}</b> — {body}{mark}")
    lines.append("<blockquote expandable>" + "\n".join(rows) + "</blockquote>")
    lines.append(f"🔔 Своя черга й нагадування за 30 хв — @{bot_username}")
    return "\n".join(lines)


# ---------- моменти перемикання ----------
def timeline(today_q: dict[str, Slots], tomorrow_q: dict[str, Slots] | None) -> dict[str, list[list[int]]]:
    """{черга: [[початок, кінець], ...]} у хвилинах від сьогоднішньої півночі, сусідні злиті."""
    out = {}
    for q in QUEUES:
        spans = [[minutes(a), minutes(b)] for a, b in today_q.get(q) or []]
        spans += [[1440 + minutes(a), 1440 + minutes(b)] for a, b in (tomorrow_q or {}).get(q) or []]
        merged: list[list[int]] = []
        for s in sorted(spans):
            if merged and s[0] <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], s[1])
            else:
                merged.append(s)
        out[q] = merged
    return out


def switch_events(tl: dict[str, list[list[int]]]) -> dict[int, dict]:
    """{момент: {"off": {черга: до_коли}, "on": [черги]}}.

    Кінець о 24:00 завтра (2880) не рахуємо: що буде далі — невідомо. Якщо завтра
    починається з відключення, timeline() уже злив його з сьогоднішнім, і «увімкнення»
    опівночі не з'явиться.
    """
    events: dict[int, dict] = {}
    for q, spans in tl.items():
        for s, e in spans:
            if s > 0:  # 00:00 сьогодні — продовження вчорашнього, його оголошували вчора
                events.setdefault(s, {"off": {}, "on": []})["off"][q] = e
            if e < 2880:
                events.setdefault(e, {"off": {}, "on": []})["on"].append(q)
    return events


def still_off(tl: dict[str, list[list[int]]], t: int) -> dict[str, int]:
    """Черги, які в момент t вже були без світла й лишаються: {черга: до_коли}."""
    return {q: e for q, spans in tl.items() for s, e in spans if s < t < e}


def _until(t: int, base: int) -> str:
    """Час кінця відносно моменту base: «18:00», «24:00» або «08:00 завтра»."""
    if t % 1440 == 0 and (t - 1) // 1440 == base // 1440:
        return "24:00"
    return f"{hhmm(t)} завтра" if t // 1440 > base // 1440 else hhmm(t)


def switch_post(t: int, event: dict, tl: dict[str, list[list[int]]]) -> str:
    lines = [f"⚡️ <b>О {hhmm(t)}</b> · Житомир", ""]
    if event["off"]:
        lines.append("🔴 <b>Вимикаємо</b>")
        by_end: dict[int, list[str]] = {}
        for q, e in sorted(event["off"].items()):
            by_end.setdefault(e, []).append(q)
        lines += [f"• <b>{', '.join(qs)}</b> — до {_until(e, t)}" for e, qs in sorted(by_end.items())]
        lines.append("")
    if event["on"]:
        lines.append("🟢 <b>Вмикаємо</b>")
        lines.append(f"• <b>{', '.join(sorted(event['on']))}</b>")
        lines.append("")
    rest = still_off(tl, t)
    if rest:
        lines.append("⏳ Ще без світла: " + ", ".join(
            f"{q} (до {_until(e, t)})" for q, e in sorted(rest.items())))
    return "\n".join(lines).strip()


# ---------- особисті повідомлення ----------
def reminder_off(q: str, start: int, end: int, now: int) -> str:
    left = start - now
    when = "Зараз" if left <= 1 else f"Через {left} хв"
    return (f"⏰ <b>{when} вимкнуть світло</b>\n"
            f"Черга {q} · {hhmm(start)}–{_until(end, start)} ({hours(end - start)})\n\n"
            "Зарядіть телефон і павербанк 🔋")


def reminder_on(q: str, t: int, next_off: int | None) -> str:
    text = f"💡 <b>Світло має повернутись</b>\nЧерга {q} · за графіком о {hhmm(t)}"
    if next_off is not None:
        day = "сьогодні" if next_off < 1440 else "завтра"
        text += f"\n\nНаступне відключення: {day} о {hhmm(next_off)}"
    return text


def personal_change(q: str, day: date, today: date, slots: Slots, prev: Slots | None) -> str:
    when = day_inline(day, today)
    if prev and not slots:
        head = f"✅ <b>Черга {q}: відключення {when} скасовано</b>"
    elif prev:
        head = f"🔄 <b>Черга {q}: змінився графік {when}</b>"
    else:
        head = f"⚡️ <b>Черга {q}: графік {when}</b>"
    lines = [head]
    if slots:
        lines.append(f"🔴 {ranges(slots)} · {hours(off_minutes(slots))} без світла")
    if prev:
        lines.append(f"<s>було: {ranges(prev) or 'світло є'}</s>")
    if slots:
        lines += ["", hour_bar(slots)]
    return "\n".join(lines)


def personal_day(q: str, day: date, today: date, slots: Slots | None) -> str:
    when = day_inline(day, today)[0].upper() + day_inline(day, today)[1:]
    if slots is None:
        return f"<b>{q}</b> · {when}: графік ще не опубліковано"
    if not slots:
        return f"<b>{q}</b> · {when}: 🟢 відключень немає"
    return (f"<b>{q}</b> · {when}: 🔴 {ranges(slots)} ({hours(off_minutes(slots))})\n"
            f"{hour_bar(slots)}")


def tomorrow(d: date) -> date:
    return d + timedelta(days=1)


# Демо-графік для /preview і /testpost, коли реальних відключень немає.
DEMO: dict[str, Slots] = {
    "1.1": [("08:00", "10:00"), ("18:30", "20:30")], "1.2": [("16:30", "20:30")],
    "2.1": [("16:30", "20:30")], "2.2": [("08:00", "10:00"), ("16:30", "20:30")],
    "3.1": [("20:00", "24:00")], "3.2": [("09:30", "13:30"), ("20:00", "24:00")],
    "4.1": [("09:30", "13:30"), ("20:00", "22:00")], "4.2": [("09:30", "13:30"), ("20:00", "24:00")],
    "5.1": [("16:00", "19:00")], "5.2": [("13:00", "17:00")],
    "6.1": [], "6.2": [("00:00", "00:30"), ("06:00", "10:00"), ("13:00", "17:00")],
}
