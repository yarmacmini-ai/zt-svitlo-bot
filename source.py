"""Джерела графіків. Кожне повертає нормалізований формат:

    {"2026-09-25": {"1.1": [("08:00", "12:00")], "1.2": [], ...}, ...}

Порожній список = черга без відключень у цей день.
"""
import json
from pathlib import Path

Schedule = dict[str, dict[str, list[tuple[str, str]]]]

ZTOE_URL = "https://ztoe-poweron.inneti.net/"
USER_AGENT = "zt-svitlo-bot/0.1 (+контакт власника в описі бота)"


class DemoSource:
    """Локальний файл — щоб перевірити бот до підключення сайту.
    Відредагуйте data/demo.json під час роботи бота, і він розішле зміни."""

    def __init__(self, path: str = "data/demo.json"):
        self.path = Path(path)

    async def fetch(self) -> Schedule:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return {day: {q: [tuple(s) for s in slots] for q, slots in queues.items()}
                for day, queues in raw.items()}


async def capture_ztoe_json() -> list[dict]:
    """Відкриває сторінку обленерго і збирає всі JSON-відповіді її API.
    Сайт — односторінковий застосунок, дані приходять запитами з браузера."""
    from playwright.async_api import async_playwright

    captured: list[dict] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(user_agent=USER_AGENT)

        async def on_response(resp):
            ctype = resp.headers.get("content-type") or ""
            if "inneti.net" in resp.url and "json" in ctype:
                try:
                    captured.append({"url": resp.url, "data": await resp.json()})
                except Exception:
                    pass

        page.on("response", on_response)
        await page.goto(ZTOE_URL, wait_until="networkidle", timeout=60_000)
        await browser.close()
    return captured


def normalize_ztoe(payloads: list[dict]) -> Schedule:
    """Перетворює відповіді API у Schedule.

    Структуру відповідей ще треба побачити: запустіть `python discover.py`,
    надішліть файли з папки discovery/ — і тут з'явиться мапінг.
    """
    raise NotImplementedError(
        "Мапінг ztoe ще не написаний: запустіть discover.py і надішліть результат.")


class ZtoeSource:
    async def fetch(self) -> Schedule:
        return normalize_ztoe(await capture_ztoe_json())


def make_source(name: str):
    return {"demo": DemoSource, "ztoe": ZtoeSource}[name]()
