"""Джерела графіків. Кожне повертає нормалізований формат:

    {"2026-09-25": {"1.1": [("08:00", "12:00")], "1.2": [], ...}, ...}

Порожній список = черга без відключень у цей день.
"""
import json
import logging
import re
from datetime import datetime
from pathlib import Path

import httpx

Schedule = dict[str, dict[str, list[tuple[str, str]]]]

log = logging.getLogger("svitlo")

ZTOE_URL = "https://www.ztoe.com.ua/unhooking-search.php"
USER_AGENT = "zt-svitlo-bot/0.2 (Zhytomyr outage schedule Telegram bot)"


class DemoSource:
    """Локальний файл — щоб перевірити бот до підключення сайту.
    Відредагуйте data/demo.json під час роботи бота, і він розішле зміни."""

    def __init__(self, path: str = "data/demo.json"):
        self.path = Path(path)

    async def fetch(self) -> Schedule:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return {day: {q: [tuple(s) for s in slots] for q, slots in queues.items()}
                for day, queues in raw.items()}


# Таблиця «Графіки погодинних відключень»: рядок на підчергу, 48 клітинок
# по пів години на кожну дату в заголовку. Червоний фон = відключення.
_TABLE = re.compile(r"<table[^>]*>(.*?)</table>", re.S | re.I)
_DATE = re.compile(r"<b[^>]*>\s*(\d\d\.\d\d\.\d{4})\s*</b>")
_ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_QUEUE = re.compile(r">\s*([1-6]\.[12])\s*</b>")
_CELL = re.compile(r"<td[^>]*background:\s*([#\w]+)[^>]*>\s*<a[^>]*>(?:&nbsp;|\s)*</a>",
                   re.S | re.I)
_WHITE = {"white", "#fff", "#ffffff"}
_RED = {"red", "#f00", "#ff0000"}
SLOTS_PER_DAY = 48


def _half_hour(i: int) -> str:
    return f"{i // 2:02d}:{'30' if i % 2 else '00'}"


def _to_ranges(off: list[bool]) -> list[tuple[str, str]]:
    """[F, T, T, F] -> [("00:30", "01:30")]: зливає сусідні півгодини."""
    ranges, start = [], None
    for i, is_off in enumerate(off + [False]):
        if is_off and start is None:
            start = i
        elif not is_off and start is not None:
            ranges.append((_half_hour(start), "24:00" if i == SLOTS_PER_DAY else _half_hour(i)))
            start = None
    return ranges


def parse_ztoe(html: str) -> Schedule:
    for table in _TABLE.findall(html):
        if "pidcherga_id" not in table:
            continue
        days = [datetime.strptime(d, "%d.%m.%Y").date().isoformat()
                for d in _DATE.findall(table)]
        if not days:
            continue
        schedule: Schedule = {day: {} for day in days}
        for row in _ROW.findall(table):
            q = _QUEUE.search(row)
            if not q:
                continue
            colors = [c.lower() for c in _CELL.findall(row)]
            if len(colors) != SLOTS_PER_DAY * len(days):
                raise ValueError(f"Черга {q.group(1)}: {len(colors)} клітинок "
                                 f"на {len(days)} дн., структура сайту змінилась")
            unknown = set(colors) - _WHITE - _RED
            if unknown:  # новий колір (напр. «можливе відключення») — рахуємо як відключення
                log.warning("Невідомі кольори клітинок: %s", unknown)
            for n, day in enumerate(days):
                chunk = colors[n * SLOTS_PER_DAY:(n + 1) * SLOTS_PER_DAY]
                schedule[day][q.group(1)] = _to_ranges([c not in _WHITE for c in chunk])
        if all(len(qs) == 12 for qs in schedule.values()):
            return schedule
        raise ValueError(f"Знайдено черг: {[len(qs) for qs in schedule.values()]}, очікувалось 12")
    raise ValueError("Таблицю графіків на сторінці не знайдено")


class ZtoeSource:
    """Сторінка Житомиробленерго з таблицею черг. Звичайний HTML у windows-1251."""

    async def fetch(self) -> Schedule:
        async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT},
                                     follow_redirects=True) as client:
            resp = await client.get(ZTOE_URL)
            resp.raise_for_status()
        return parse_ztoe(resp.content.decode("cp1251", errors="replace"))


def make_source(name: str):
    return {"demo": DemoSource, "ztoe": ZtoeSource}[name]()
