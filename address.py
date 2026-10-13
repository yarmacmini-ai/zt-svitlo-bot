"""Пошук черги за адресою через форму на сайті Житомиробленерго — по всій області.

Форма на unhooking-search.php: РЕМ (район) -> населений пункт -> вулиця, кожен крок —
POST. Сайт віддає таблицю «вулиця · будинки -> черга/підчерга». Вже після вибору
населеного пункту таблиця містить усі його адреси, тож для сіл вулицю можна не питати.
Графік один на всю область: 12 підчерг 1.1–6.2.
"""
import html
import re
import time

import httpx

from source import USER_AGENT, ZTOE_URL

# Житомир — за замовчуванням. Id сайт може перенумерувати (так було в жовтні 2026),
# тому беремо його щодня зі списку Житомирського РЕМ за назвою, див. ensure_zhytomyr().
ZHYTOMYR = ("7", "16657")
ZHYTOMYR_REM, ZHYTOMYR_NAME = "7", "місто житомир"
CACHE_TTL = 24 * 3600
RECORDS_TTL = 6 * 3600
ALL_LIMIT = 400  # більше записів у населеному пункті (міста) — питаємо вулицю

_OPTION = re.compile(r'<option value="(\d+)"[^>]*>([^<]*)')
_ROW = re.compile(r"<tr>((?:\s*<td[^>]*>.*?</td>){7})\s*</tr>", re.S)
_TD = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TOTAL = re.compile(r"знайдено записів:\s*(\d+)")
_PREFIX = re.compile(
    r"^(вул\.?|вулиця|пров\.?|провулок|проїзд|бульвар|б\.?|просп\.?|проспект|"
    r"шосе|майдан|площа|пл\.?|узвіз|тупик|алея|автошлях)\s+")
_PLACE_PREFIX = re.compile(r"^(с\.|село|селище|смт\.?|м\.|місто|с/рада|с-ще)\s*")

_cache: dict[tuple, tuple[float, object]] = {}


async def _post(data: dict) -> str:
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": USER_AGENT},
                                 follow_redirects=True) as client:
        resp = await client.post(ZTOE_URL, data=data)
        resp.raise_for_status()
    return resp.content.decode("cp1251", errors="replace")


def _cached(key: tuple, ttl: int):
    hit = _cache.get(key)
    return hit[1] if hit and time.time() - hit[0] < ttl else None


def _options(page: str, name: str) -> list[tuple[str, str]]:
    select = re.search(rf'<select name="{name}".*?</select>', page, re.S)
    if not select:
        return []
    return [(i, html.unescape(n).strip()) for i, n in _OPTION.findall(select.group(0)) if i != "0"]


def _norm(name: str) -> str:
    name = name.lower().replace("’", "'").replace("ʼ", "'").replace("`", "'")
    name = name.replace("ё", "е")
    name = re.sub(r"\s+", " ", name).strip()
    return _PREFIX.sub("", name)


def _norm_place(name: str) -> str:
    return _PLACE_PREFIX.sub("", _norm(name)).strip()


def _rank(items, key, q):
    """Спочатку точні збіги, потім з початку слова, потім входження."""
    exact, prefix, inside = [], [], []
    for it in items:
        n = key(it)
        if n == q:
            exact.append(it)
        elif n.startswith(q) or f" {q}" in n or f"-{q}" in n:
            prefix.append(it)
        elif q in n:
            inside.append(it)
    return exact, prefix, inside


# ---------- населені пункти ----------
async def ensure_zhytomyr() -> tuple[str, str]:
    """Актуальний id міста Житомир (кеш на добу)."""
    global ZHYTOMYR
    if _cached(("zhytomyr",), CACHE_TTL):
        return ZHYTOMYR
    for cid, name in _options(await _post({"rem_id": ZHYTOMYR_REM}), "naspunkt_id"):
        if _norm(name) == ZHYTOMYR_NAME:
            ZHYTOMYR = (ZHYTOMYR_REM, cid)
            _cache[("zhytomyr",)] = (time.time(), True)
            break
    else:
        raise ValueError("Місто Житомир у списку сайту не знайдено")
    return ZHYTOMYR


async def settlements() -> list[dict]:
    """Усі ~1760 населених пунктів області: {rem, rem_name, id, name}. Кеш на добу.
    Перший виклик — 20 запитів до сайту, тому бот прогріває кеш при старті."""
    found = _cached(("places",), CACHE_TTL)
    if found:
        return found
    rems = _options(await _post({}), "rem_id")
    found = []
    for rem, rem_name in rems:
        for cid, name in _options(await _post({"rem_id": rem}), "naspunkt_id"):
            found.append({"rem": rem, "rem_name": rem_name, "id": cid, "name": name})
    if not found:
        raise ValueError("Список населених пунктів на сайті не знайдено")
    _cache[("places",)] = (time.time(), found)
    return found


def place_label(p: dict) -> str:
    """«с. Березівка (Хорошівський р-н)»; Житомир — без району."""
    if (p["rem"], p["id"]) == ZHYTOMYR:
        return "м. Житомир"
    return f"{p['name']} ({p['rem_name']} р-н)"


async def find_place(rem: str, cid: str) -> dict | None:
    if (rem, cid) == ZHYTOMYR:
        return {"rem": rem, "rem_name": "Житомирський", "id": cid, "name": "місто Житомир"}
    return next((p for p in await settlements() if p["rem"] == rem and p["id"] == cid), None)


async def search_places(query: str, limit: int = 8) -> tuple[list[dict], bool]:
    """(збіги, чи є точний збіг назви). Міста й селища — вище за села з тією ж назвою."""
    q = _norm_place(query)
    if len(q) < 2:
        return [], False
    exact, prefix, inside = _rank(await settlements(), lambda p: _norm_place(p["name"]), q)
    town = lambda p: not p["name"].lower().startswith(("с.", "село", "с/рада"))  # noqa: E731
    exact.sort(key=lambda p: not town(p))
    return (exact + prefix + inside)[:limit], bool(exact)


# ---------- вулиці й записи ----------
async def streets(rem: str, cid: str) -> list[tuple[str, str]]:
    """[(id, "вул. Київська"), ...] для населеного пункту — кеш на добу."""
    found = _cached(("streets", rem, cid), CACHE_TTL)
    if found is None:
        found = _options(await _post({"rem_id": rem, "naspunkt_id": cid}), "vulica_id")
        _cache[("streets", rem, cid)] = (time.time(), found)
    return found


async def street_name(rem: str, cid: str, street_id: str) -> str | None:
    return next((n for i, n in await streets(rem, cid) if i == street_id), None)


async def search_streets(query: str, rem: str, cid: str, limit: int = 8) -> list[tuple[str, str]]:
    q = _norm(query)
    if len(q) < 2:
        return []
    exact, prefix, inside = _rank(await streets(rem, cid), lambda s: _norm(s[1]), q)
    return (exact or prefix + inside)[:limit]  # точний збіг — без зайвого перепитування


def _queue(cherga: str, pidcherga: str) -> str | None:
    if cherga.isdigit() and pidcherga.isdigit():
        return f"{cherga}.{pidcherga}"
    return None  # «В цей час не вимикається…»


def _parse_records(page: str) -> list[dict]:
    start = page.find("знайдено записів")  # таблиця результатів іде одразу після цього
    table = page[start:page.find("</table>", start)] if start >= 0 else ""
    records = []
    for row in _ROW.findall(table):
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in _TD.findall(row)]
        if cells[0] == "РЕМ":
            continue
        records.append({
            "street": cells[2],
            "houses": [h.strip() for h in cells[3].split(",") if h.strip()],
            "queue": _queue(cells[4], cells[5]),
            "kind": cells[6],
        })
    total = _TOTAL.search(page)
    if total and int(total.group(1)) != len(records):
        raise ValueError(f"Розібрано {len(records)} з {total.group(1)} записів — структура сайту змінилась")
    return records


async def street_records(rem: str, cid: str, street_id: str) -> list[dict]:
    """[{"street", "houses": ["19", "19/2"], "queue": "1.2" | None, "kind": "побутові споживачі"}]"""
    key = ("records", rem, cid, street_id)
    found = _cached(key, RECORDS_TTL)
    if found is None:
        # Без "all" сайт показує лише 10 записів і кнопку «показати ще N записів».
        found = _parse_records(await _post(
            {"rem_id": rem, "naspunkt_id": cid, "vulica_id": street_id, "all": "1"}))
        _cache[key] = (time.time(), found)
    return found


async def place_records(rem: str, cid: str) -> list[dict] | None:
    """Усі адреси населеного пункту, якщо їх небагато (села). None — для великих міст."""
    key = ("place_records", rem, cid)
    found = _cached(key, RECORDS_TTL)
    if found is None:
        page = await _post({"rem_id": rem, "naspunkt_id": cid})
        _cache[("streets", rem, cid)] = (time.time(), _options(page, "vulica_id"))
        total = _TOTAL.search(page)
        if not total or int(total.group(1)) > ALL_LIMIT:
            return None
        if "показати ще" in page:
            page = await _post({"rem_id": rem, "naspunkt_id": cid, "all": "1"})
        found = _parse_records(page)
        _cache[key] = (time.time(), found)
    return found


def place_queues(records: list[dict]) -> list[str]:
    """Черги мешканців населеного пункту (побутові споживачі), у порядку появи."""
    home = [r for r in records if r["kind"] == "побутові споживачі"] or records
    return list(dict.fromkeys(r["queue"] for r in home if r["queue"]))


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


def split_place(text: str) -> tuple[str | None, str]:
    """"Бердичів, Житомирська 12" -> ("Бердичів", "Житомирська 12"); без коми — (None, text)."""
    if "," not in text:
        return None, text.strip()
    place, rest = text.split(",", 1)
    return place.strip(), rest.strip()
