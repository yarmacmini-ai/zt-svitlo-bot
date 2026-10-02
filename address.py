"""Пошук черги за адресою через форму на сайті Житомиробленерго.

Форма на unhooking-search.php: РЕМ -> населений пункт -> вулиця, кожен крок —
POST. На останньому кроці сайт віддає таблицю «будинки -> черга/підчерга».
Шукаємо лише по місту Житомир.
"""
import html
import re
import time

import httpx

from source import USER_AGENT, ZTOE_URL

REM_ID, CITY_ID = "7", "15647"  # Житомирський РЕМ, місто Житомир
STREETS_TTL = 24 * 3600
RECORDS_TTL = 6 * 3600

_OPTION = re.compile(r'<option value="(\d+)"[^>]*>([^<]*)')
_ROW = re.compile(r"<tr>((?:\s*<td[^>]*>.*?</td>){7})\s*</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_PREFIX = re.compile(
    r"^(вул\.?|вулиця|пров\.?|провулок|проїзд|бульвар|б\.?|просп\.?|проспект|"
    r"шосе|майдан|площа|пл\.?|узвіз|тупик|алея|автошлях)\s+")

_streets: tuple[float, list[tuple[str, str]]] = (0.0, [])
_records: dict[str, tuple[float, list[dict]]] = {}


async def _post(data: dict) -> str:
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT},
                                 follow_redirects=True) as client:
        resp = await client.post(ZTOE_URL, data=data)
        resp.raise_for_status()
    return resp.content.decode("cp1251", errors="replace")


def _norm(name: str) -> str:
    name = name.lower().replace("’", "'").replace("ʼ", "'").replace("`", "'")
    name = name.replace("ё", "е")
    name = re.sub(r"\s+", " ", name).strip()
    return _PREFIX.sub("", name)


async def streets() -> list[tuple[str, str]]:
    """[(id, "вул. Київська"), ...] — кеш на добу."""
    global _streets
    if time.time() - _streets[0] < STREETS_TTL and _streets[1]:
        return _streets[1]
    page = await _post({"rem_id": REM_ID, "naspunkt_id": CITY_ID})
    select = re.search(r'<select name="vulica_id".*?</select>', page, re.S)
    if not select:
        raise ValueError("Список вулиць на сайті не знайдено")
    found = [(i, html.unescape(n).strip()) for i, n in _OPTION.findall(select.group(0)) if i != "0"]
    _streets = (time.time(), found)
    return found


async def street_name(street_id: str) -> str | None:
    return next((n for i, n in await streets() if i == street_id), None)


async def search_streets(query: str, limit: int = 8) -> list[tuple[str, str]]:
    """Збіги за назвою: спочатку точні, потім з початку слова, потім входження."""
    q = _norm(query)
    if len(q) < 2:
        return []
    exact, prefix, inside = [], [], []
    for sid, name in await streets():
        n = _norm(name)
        if n == q:
            exact.append((sid, name))
        elif n.startswith(q) or f" {q}" in n:
            prefix.append((sid, name))
        elif q in n:
            inside.append((sid, name))
    return (exact + prefix + inside)[:limit]


def _queue(cherga: str, pidcherga: str) -> str | None:
    if cherga.isdigit() and pidcherga.isdigit():
        return f"{cherga}.{pidcherga}"
    return None  # «В цей час не вимикається…»


async def street_records(street_id: str) -> list[dict]:
    """[{"houses": ["19", "19/2"], "queue": "1.2" | None, "kind": "побутові споживачі"}]"""
    cached = _records.get(street_id)
    if cached and time.time() - cached[0] < RECORDS_TTL:
        return cached[1]
    # Без "all" сайт показує лише 10 записів і кнопку «показати ще N записів».
    page = await _post({"rem_id": REM_ID, "naspunkt_id": CITY_ID, "vulica_id": street_id,
                        "all": "1"})
    start = page.find("знайдено записів")  # таблиця результатів іде одразу після цього
    table = page[start:page.find("</table>", start)] if start >= 0 else ""
    records = []
    for row in _ROW.findall(table):
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in _TD.findall(row)]
        if cells[0] == "РЕМ":
            continue
        records.append({
            "houses": [h.strip() for h in cells[3].split(",") if h.strip()],
            "queue": _queue(cells[4], cells[5]),
            "kind": cells[6],
        })
    total = re.search(r"знайдено записів:\s*(\d+)", page)
    if total and int(total.group(1)) != len(records):
        raise ValueError(f"Вулиця {street_id}: розібрано {len(records)} з {total.group(1)} записів")
    _records[street_id] = (time.time(), records)
    return records


def _house_key(h: str) -> str:
    # Латинські літери, що виглядають як кириличні, і регістр: "31a" == "31А"
    return h.upper().replace(" ", "").translate(str.maketrans("ABCEHIKMOPTX", "АВСЕНІКМОРТХ"))


def find_house(records: list[dict], house: str) -> list[dict]:
    """Записи для будинку. Побутових споживачів ставимо першими: бот для мешканців."""
    key = _house_key(house)
    hits = [r for r in records if key in map(_house_key, r["houses"])]
    hits.sort(key=lambda r: r["kind"] != "побутові споживачі")
    return hits


def split_address(text: str) -> tuple[str, str | None]:
    """"Київська 24" -> ("Київська", "24"); "Київська" -> ("Київська", None)."""
    m = re.match(r"^(.*?\D)[\s,]+(\d+[\w/\-]*)\s*$", text.strip())
    if m and len(m.group(1).strip()) >= 2:
        return m.group(1).strip(" ,"), m.group(2)
    return text.strip(), None
