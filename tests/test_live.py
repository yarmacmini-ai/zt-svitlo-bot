"""Перевірки з мережею: сайт Житомиробленерго.  python tests/test_live.py"""
import asyncio, os, sys, time
from pathlib import Path
os.environ.update(BOT_TOKEN="1:x", DB_PATH=":memory:", SOURCE="demo")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bot, address
from source import ZtoeSource
log = []
class Msg:
    def __init__(s, text=""): s.text = text
    async def reply_text(s, text, parse_mode=None, reply_markup=None):
        btns = [(b.text, b.callback_data) for r in reply_markup.inline_keyboard for b in r] if reply_markup else []
        log.append((text, btns)); print("BOT:", text.replace("\n", " | ")[:150]); [print("    [", t, "]") for t, _ in btns[:9]]
class U: id = 1
class Q:
    def __init__(s, d): s.data, s.from_user, s.message = d, U(), Msg()
    async def answer(s, *a, **k): pass
    async def edit_message_reply_markup(s, m): pass
class Upd:
    def __init__(s, text=None, data=None):
        s.effective_message, s.effective_user = Msg(text), U(); s.callback_query = Q(data) if data else None
class Ctx: user_data = {}
def btn(part):
    for t, d in log[-1][1]:
        if part in t: return d
    raise AssertionError(f"нема кнопки {part}: {log[-1][1]}")
async def say(t): print("\nUSER:", t); await bot.on_text(Upd(text=t), Ctx)
async def press(part):
    d = btn(part); print("USER [", part, "]")
    h = {"c": bot.on_place, "s": bot.on_street, "sub": bot.on_subscribe, "menu": bot.on_menu}[d.split(":")[0]]
    await h(Upd(data=d), Ctx)
async def main():
    src = ZtoeSource(); sched = await src.fetch()
    assert sched and all(len(q) == 12 for q in sched.values()), sched
    print("графік:", {d: sum(map(len, q.values())) for d, q in sched.items()}, "команда:", src.commands)
    t = time.time(); n = len(await address.settlements()); print("населених пунктів:", n, round(time.time() - t, 1), "с")
    await say("Київська 24"); assert "Ваша черга" in log[-1][0]
    await say("Бердичів, Житомирська 12")
    await say("Бердичів")
    await say("Березівка"); assert "Який саме" in log[-1][0]
    await press("Хорошівський")
    await say("Маркуші")
    await say("Коростень, Грушевського")
    print("USER [ 🔎 ]"); await bot.on_menu(Upd(data="menu:addr"), Ctx)
    await say("Київська, 24"); assert "Ваша черга" in log[-1][0], "кома між вулицею і будинком"
    await say("Абвгдеївка, Шевченка 1"); assert "Не знайшов населений" in log[-1][0]
    print("\nOK")
asyncio.run(main())
