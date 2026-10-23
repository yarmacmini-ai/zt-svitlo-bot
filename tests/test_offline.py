"""Перевірки без мережі: фейковий Telegram, демо-графік.  python tests/test_offline.py"""
import asyncio
import os
import sys
from datetime import date, datetime
from pathlib import Path

os.environ.update(BOT_TOKEN="1:x", DB_PATH=":memory:", CHANNEL_ID="-1003948683401", SOURCE="demo")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(Path(__file__).resolve().parent.parent)

from telegram.ext import ApplicationHandlerStop  # noqa: E402

import bot  # noqa: E402
import posts  # noqa: E402

T, TM = date(2026, 10, 13), date(2026, 10, 14)
CH, ADMIN, USER = "-1003948683401", 7, 8
sent = []
silent: dict = {}
BLOCKED = {99}
tasks = []


# ---------- фейковий Telegram ----------
class Chat:
    def __init__(self, i): self.id, self.username = i, None


class Msg:
    def __init__(self, text=None, chat=ADMIN, mid=500):
        self.text, self.chat_id, self.message_id, self.chat = text, chat, mid, Chat(chat)

    async def reply_text(self, text, **k):
        sent.append(("reply", self.chat_id, text, _buttons(k.get("reply_markup"))))


def _buttons(kb):
    rows = getattr(kb, "inline_keyboard", None) or getattr(kb, "keyboard", None) or []
    return [(b.text, getattr(b, "callback_data", None)) for r in rows for b in r]


class FakeBot:
    username = "zt_bot"
    n = 900

    def _msg(self, chat):
        FakeBot.n += 1
        return Msg(chat=chat, mid=FakeBot.n)

    async def get_chat_member(self, chat, uid):
        return type("M", (), {"status": "administrator" if uid == ADMIN else "member"})

    async def send_message(self, cid, text, reply_markup=None, reply_parameters=None, **k):
        sent.append(("msg", cid, text, reply_parameters and reply_parameters.message_id))
        silent[cid] = k.get("disable_notification", False)
        if cid == CH and ("Вимикаємо" in text or "Вмикаємо" in text):
            assert k.get("disable_notification"), "пости перемикань — без звуку"
        return self._msg(cid)

    async def send_photo(self, cid, photo, caption=None, reply_parameters=None, **k):
        assert photo[:4] == b"\x89PNG" and len(caption) <= 1024
        sent.append(("photo", cid, caption, reply_parameters and reply_parameters.message_id))
        return self._msg(cid)

    async def edit_message_media(self, media, chat_id=None, message_id=None, reply_markup=None):
        assert media.media and len(media.caption) <= 1024
        sent.append(("edit_media", chat_id, media.caption, message_id))
        return Msg(chat=chat_id, mid=message_id)

    async def copy_message(self, to, frm, mid, reply_markup=None, disable_notification=False):
        if to in BLOCKED:
            from telegram.error import Forbidden
            raise Forbidden("bot was blocked by the user")
        if disable_notification:
            silent[to] = True
        sent.append(("copy", to, mid, bool(reply_markup)))
        return self._msg(to)

    async def edit_message_reply_markup(self, chat, mid, reply_markup=None):
        sent.append(("edit", mid, bool(reply_markup)))


class Query:
    def __init__(self, data, uid):
        self.data, self.message, self.from_user = data, Msg(chat=uid), type("U", (), {"id": uid})

    async def answer(self, *a, **k): pass

    async def edit_message_reply_markup(self, markup): pass


class Upd:
    def __init__(self, text=None, data=None, uid=ADMIN, mid=500):
        self.effective_message = Msg(text, uid, mid)
        self.effective_user = type("U", (), {"id": uid})
        self.effective_chat = Chat(uid)
        self.callback_query = Query(data, uid) if data else None


class Ctx:
    bot = FakeBot()
    user_data: dict = {}
    application = type("App", (), {"create_task": staticmethod(lambda coro: tasks.append(coro))})


def at(d: date, h: int, m: int):
    bot.today = lambda: d
    bot.datetime = type("DT", (), {"now": staticmethod(lambda tz=None: datetime(d.year, d.month, d.day, h, m, tzinfo=tz))})


# ---------- чисті функції ----------
def test_posts():
    tl = posts.timeline({"1.1": [("22:00", "24:00")]}, {"1.1": [("00:00", "02:00"), ("08:00", "10:00")]})
    assert tl["1.1"] == [[1320, 1560], [1920, 2040]]
    ev = posts.switch_events(tl)
    assert ev[1320]["off"] == {"1.1": 1560} and 1440 not in ev  # через північ — без «увімкнення»
    assert posts._until(1560, 1320) == "02:00 завтра" and posts._until(1440, 1320) == "24:00"
    assert posts.switch_events(posts.timeline({"2.1": [("20:00", "24:00")]}, {}))[1440]["on"] == ["2.1"]
    cap = posts.channel_caption(TM, T, posts.DEMO, "zt_bot", command=[("08:00", "16:00", "1,5 черги")])
    assert len(cap) < 1024 and "<blockquote expandable>" in cap and "⚙️ Укренерго: 8–16 — 1,5 черги" in cap
    assert "<b>1.2 · 2.1</b> — 16:30–20:30 · 4 год" in cap  # однакові черги в один рядок
    assert "Житомирщина" in posts.switch_post(1200, ev.get(1320) or {"off": {"1.1": 1560}, "on": []}, tl)


# ---------- розсилка, канал, перемикання ----------
async def test_flow():
    bot.store.set_meta("mode", "hard")
    at(T, 12, 0)
    bot.store.toggle(1, "2.1"); bot.store.toggle(2, "5.2"); bot.store.set_reminders(2, False)
    sched = {T.isoformat(): {q: [] for q in posts.QUEUES}}

    async def src(): return sched
    bot.source.fetch = src
    await bot.poll(Ctx); assert not sent, sent  # білий день — тиша

    sched[TM.isoformat()] = dict(posts.DEMO)
    await bot.poll(Ctx)
    assert {(k, c) for k, c, *_ in sent} == {("photo", CH), ("msg", 1), ("msg", 2)}, sent
    first = bot.store.get_meta(f"post:{TM.isoformat()}")
    sent.clear()

    sched[TM.isoformat()] = dict(posts.DEMO, **{"2.1": [("17:00", "21:00")]})
    await bot.poll(Ctx)
    photo = next(x for x in sent if x[0] == "photo")
    assert "Оновлено" in photo[2] and "<b>2.1</b> — 17–21 · 4 год ✏️" in photo[2], photo[2]
    assert photo[3] == int(first), "оновлення — відповіддю на перший пост дня"
    assert [x[1] for x in sent if x[0] == "msg"] == [1]
    sent.clear()

    at(TM, 16, 30)
    await bot.tick(Ctx); await bot.tick(Ctx)  # другий тік — без дублів
    ch = [x for x in sent if x[1] == CH]
    assert len(ch) == 1 and "О 16:30" in ch[0][2] and ch[0][3] is not None, ch
    assert any(x[1] == 1 and "Через 30 хв" in x[2] for x in sent)
    assert not any(x[1] == 2 for x in sent), "у підписника 2 нагадування вимкнені"
    sent.clear()
    at(TM, 21, 0)
    await bot.tick(Ctx)
    assert any(x[1] == 1 and "Світло має повернутись" in x[2] for x in sent), sent
    sent.clear()


async def test_catchup():
    at(T, 12, 0)
    day = date(2026, 10, 20).isoformat()
    at(date(2026, 10, 20), 12, 0)
    for q, sl in posts.DEMO.items():
        bot.store.update_snapshot(day, q, sl)

    async def src(): return {day: dict(posts.DEMO)}
    bot.source.fetch = src
    await bot.poll(Ctx); await bot.poll(Ctx)
    assert [k for k, *_ in sent] == ["photo"], sent  # картка дня — один раз
    sent.clear()


# ---------- меню й ручні пости ----------
async def test_menu_and_posts():
    Ctx.user_data = {}
    await bot.cmd_start(Upd("/start"), Ctx)
    assert bot.BTN_POST in [t for t, _ in sent[-1][3]]
    await bot.cmd_start(Upd("/start", uid=USER), Ctx)
    assert bot.BTN_POST not in [t for t, _ in sent[-1][3]]

    Ctx.user_data = {}
    await bot.on_menu_button(Upd(bot.BTN_POST), Ctx)
    try:
        await bot.on_compose(Upd("<b>Важливо</b>", mid=501), Ctx)
        raise AssertionError("чернетка має зупиняти інші обробники")
    except ApplicationHandlerStop:
        pass
    preview_id = sent[-2][0] == "copy" and FakeBot.n
    await bot.on_post_action(Upd(data="post:btn"), Ctx)
    assert sent[-1] == ("edit", preview_id, False)
    await bot.on_post_action(Upd(data="post:pub"), Ctx)
    assert sent[-2] == ("copy", CH, 501, False) and "t.me/c/3948683401/" in sent[-1][2]

    Ctx.user_data = {}
    await bot.on_menu_button(Upd(bot.BTN_POST, uid=USER), Ctx)
    assert "лише для адмінів" in sent[-1][2] and not Ctx.user_data.get("compose")
    await bot.on_compose(Upd("Київська 24", uid=USER), Ctx)  # не чернетка — пропускає далі
    sent.clear()


async def test_light():
    """Легкий режим: тихе повідомлення раз на день, редагування поста, без перемикань."""
    bot.store.set_meta("mode", "light")
    D = date(2026, 10, 25)
    at(date(2026, 10, 24), 12, 0)
    bot.store.toggle(3, "2.1")
    sched = {D.isoformat(): dict(posts.DEMO)}

    async def src(): return sched
    bot.source.fetch = src
    await bot.poll(Ctx)
    photo = next(x for x in sent if x[0] == "photo")
    assert "можливих відключень" in photo[2] and "сподіваємось" in photo[2], photo[2]
    personal = [x for x in sent if x[0] == "msg" and x[1] == 3]
    assert len(personal) == 1 and "можливі відключення" in personal[0][2] and silent[3], personal
    post_id = bot.store.get_meta(f"post:{D.isoformat()}")
    sent.clear()

    sched[D.isoformat()] = dict(posts.DEMO, **{"2.1": [("17:00", "21:00")]})
    await bot.poll(Ctx)
    assert [x[0] for x in sent] == ["edit_media"], sent  # той самий пост, без нових повідомлень
    assert sent[0][3] == int(post_id) and "Оновлено о" in sent[0][2]
    sent.clear()

    at(D, 16, 30)
    await bot.tick(Ctx)
    assert not sent, "у легкому режимі — без перемикань і нагадувань"

    Ctx.user_data = {}
    await bot.on_mode(Upd(data="mode:hard", uid=USER), Ctx)
    assert bot.light_mode() and "лише для адмінів" in sent[-1][2]
    await bot.on_mode(Upd(data="mode:hard"), Ctx)
    assert not bot.light_mode() and "Жорсткий" in sent[-1][2]
    sent.clear()


async def test_broadcast():
    """Розсилка в особисті: прев'ю, без звуку, фон, звіт; заблоковані випадають з аудиторії."""
    for uid in (50, 51, 99):
        bot.store.touch_user(uid)
    n = len(bot.store.audience())
    Ctx.user_data = {}
    await bot.on_menu_button(Upd(bot.BTN_BROADCAST), Ctx)
    assert Ctx.user_data["compose"] == "broadcast" and f"({n})" in sent[-1][2]
    try:
        await bot.on_compose(Upd("Привіт усім 👋", mid=777), Ctx)
        raise AssertionError("чернетка розсилки має зупиняти інші обробники")
    except ApplicationHandlerStop:
        pass
    assert sent[-2][:3] == ("copy", ADMIN, 777) and f"Надіслати {n}" in sent[-1][3][0][0]
    await bot.on_broadcast_action(Upd(data="bc:sound"), Ctx)
    sent.clear(); silent.clear()
    await bot.on_broadcast_action(Upd(data="bc:send"), Ctx)
    await bot.on_broadcast_action(Upd(data="bc:send"), Ctx)  # подвійне натискання
    assert len(tasks) == 1, "друге натискання не має запустити ще одну розсилку"
    bot.BROADCAST_PAUSE = 0
    await tasks.pop()
    copies = [x for x in sent if x[0] == "copy"]
    assert len(copies) == n - 1 and all(silent.get(x[1]) for x in copies), (copies, silent)
    assert "надіслано" in sent[-1][2] and "заблокували бота 1" in sent[-1][2], sent[-1]
    assert 99 not in bot.store.audience()
    # не адмін не може
    Ctx.user_data = {}
    await bot.on_menu_button(Upd(bot.BTN_BROADCAST, uid=USER), Ctx)
    assert "лише для адмінів" in sent[-1][2] and not Ctx.user_data.get("compose")
    sent.clear()


async def main():
    test_posts()
    await test_flow()
    await test_light()
    await test_broadcast()
    await test_catchup()
    await test_menu_and_posts()
    print("OK")


asyncio.run(main())
