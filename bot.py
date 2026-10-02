"""Бот графіків відключень Житомира.

Користувач обирає свою чергу (1.1–6.2) і отримує повідомлення лише тоді,
коли графік його черги з'являється або змінюється. У канал іде зведення
по всіх чергах при кожній зміні.
"""
import asyncio
import logging
import os
import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

import address
from source import make_source
from storage import Store

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # інакше в лог потрапляє URL з токеном
log = logging.getLogger("svitlo")

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.getenv("CHANNEL_ID") or None
POLL_MINUTES = int(os.getenv("POLL_MINUTES", "10"))

QUEUES = [f"{i}.{j}" for i in range(1, 7) for j in (1, 2)]
MONTHS = ["січня", "лютого", "березня", "квітня", "травня", "червня", "липня",
          "серпня", "вересня", "жовтня", "листопада", "грудня"]

KYIV = ZoneInfo("Europe/Kyiv")


def today() -> date:
    """Сервер живе в UTC, а графіки — за київським часом."""
    return datetime.now(KYIV).date()


SOURCE = os.getenv("SOURCE", "demo")
store = Store(os.getenv("DB_PATH", "svitlo.db"))
source = make_source(SOURCE)
if store.bind_source(SOURCE):
    log.info("Джерело змінилось на %s: старі знімки стерто", SOURCE)


# ---------- форматування ----------
def human_day(day: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d").date()
    delta = (d - today()).days
    label = {0: "сьогодні", 1: "завтра"}.get(delta)
    base = f"{d.day} {MONTHS[d.month - 1]}"
    return f"{label}, {base}" if label else base


def fmt_slots(slots) -> str:
    if not slots:
        return "без відключень"
    return ", ".join(f"{a}–{b}" for a, b in slots)


def queues_keyboard(user_id: int) -> InlineKeyboardMarkup:
    mine = set(store.user_queues(user_id))
    buttons = [InlineKeyboardButton(("✅ " if q in mine else "") + q,
                                    callback_data=f"q:{q}") for q in QUEUES]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    return InlineKeyboardMarkup(rows)


MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔎 Знайти чергу за адресою", callback_data="menu:addr")],
    [InlineKeyboardButton("Я знаю свою чергу", callback_data="menu:pick")],
])
ASK_ADDRESS = ("Напишіть вулицю і номер будинку в Житомирі, наприклад:\n"
               "<i>Київська 24</i>")
SITE_DOWN = ("Сайт Житомиробленерго зараз не відповідає, тож адресу перевірити не можу. "
             "Спробуйте пізніше або оберіть чергу вручну.")


# ---------- команди ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    mine = store.user_queues(update.effective_user.id)
    text = ("Я надсилаю графік відключень світла у Житомирі лише для вашої черги — "
            "і лише коли він з'являється або змінюється. Без зайвих повідомлень.\n\n")
    text += (f"Ваші черги: <b>{', '.join(mine)}</b>. Додати ще одну адресу?"
             if mine else "Спершу знайдемо вашу чергу. Можна просто написати адресу.")
    await update.effective_message.reply_text(text, parse_mode="HTML", reply_markup=MENU)


async def on_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "menu:addr":
        ctx.user_data.clear()
        await query.message.reply_text(ASK_ADDRESS, parse_mode="HTML")
    else:
        await query.message.reply_text(
            "Оберіть черги — можна кілька (дім, робота, батьки). "
            "Повторне натискання знімає підписку.",
            reply_markup=queues_keyboard(query.from_user.id))


async def on_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    queue = query.data.split(":", 1)[1]
    on = store.toggle(query.from_user.id, queue)
    await query.answer(f"Черга {queue}: {'підписано' if on else 'відписано'}")
    await query.edit_message_reply_markup(queues_keyboard(query.from_user.id))


# ---------- пошук черги за адресою ----------
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Будь-який текст у приваті — це адреса: «Київська 24», «Київська» або «24»."""
    msg = update.effective_message
    text = (msg.text or "").strip()
    street = ctx.user_data.get("street")
    try:
        if street and re.fullmatch(r"\d+[\w/\-]*", text):
            await show_house(msg, ctx, street, text)
            return
        query, house = address.split_address(text)
        found = await address.search_streets(query)
    except (httpx.HTTPError, ValueError):
        log.exception("Пошук адреси на сайті обленерго не вдався")
        await msg.reply_text(SITE_DOWN, reply_markup=MENU)
        return

    if not found:
        await msg.reply_text(
            f"Не знайшов вулицю «{query}» у Житомирі. Спробуйте лише назву, без «вул.», "
            "наприклад: <i>Київська</i>. Або оберіть чергу вручну.",
            parse_mode="HTML", reply_markup=MENU)
        return
    ctx.user_data["house"] = house
    if len(found) == 1:
        await pick_street(msg, ctx, found[0][0])
        return
    buttons = [[InlineKeyboardButton(name, callback_data=f"s:{sid}")] for sid, name in found]
    await msg.reply_text("Яка саме вулиця?", reply_markup=InlineKeyboardMarkup(buttons))


async def on_street(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        await pick_street(query.message, ctx, query.data.split(":", 1)[1])
    except (httpx.HTTPError, ValueError):
        log.exception("Пошук адреси на сайті обленерго не вдався")
        await query.message.reply_text(SITE_DOWN, reply_markup=MENU)


async def pick_street(msg, ctx, street_id: str):
    ctx.user_data["street"] = street_id
    house = ctx.user_data.pop("house", None)
    if house:
        await show_house(msg, ctx, street_id, house)
    else:
        name = await address.street_name(street_id)
        await msg.reply_text(f"{name}. Який номер будинку?")


async def show_house(msg, ctx, street_id: str, house: str):
    name = await address.street_name(street_id)
    records = await address.street_records(street_id)
    hits = address.find_house(records, house)
    again = InlineKeyboardButton("🔎 Інша адреса", callback_data="menu:addr")

    if not hits:
        queues = sorted({r["queue"] for r in records if r["queue"]})
        buttons = [[InlineKeyboardButton(f"Підписатися на {q}", callback_data=f"sub:{q}")]
                   for q in queues]
        await msg.reply_text(
            f"Будинку {house} немає в списку обленерго для «{name}». Напишіть інший номер"
            + (f" або оберіть чергу — на цій вулиці є: {', '.join(queues)}." if queues else "."),
            reply_markup=InlineKeyboardMarkup(buttons + [[again]]))
        return

    queues = list(dict.fromkeys(r["queue"] for r in hits if r["queue"]))
    if not queues:
        await msg.reply_text(
            f"📍 {name}, {house}\nЦей будинок не відключають за графіками — "
            "лише у разі аварій. Підписуватися не потрібно.",
            reply_markup=InlineKeyboardMarkup([[again]]))
        return
    ctx.user_data.pop("street", None)
    lines = [f"📍 {name}, {house}"]
    if len(queues) == 1:
        lines.append(f"Ваша черга: <b>{queues[0]}</b>")
    else:  # різні черги для побутових і юридичних споживачів
        lines.append("За цією адресою кілька черг:")
        lines += [f"• <b>{r['queue']}</b> — {r['kind']}" for r in hits if r["queue"]]
    buttons = [[InlineKeyboardButton(f"✅ Підписатися на {q}", callback_data=f"sub:{q}")]
               for q in queues]
    await msg.reply_text("\n".join(lines), parse_mode="HTML",
                         reply_markup=InlineKeyboardMarkup(buttons + [[again]]))


async def on_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    queue = query.data.split(":", 1)[1]
    uid = query.from_user.id
    if queue not in store.user_queues(uid):
        store.toggle(uid, queue)
    await query.answer(f"Підписано на чергу {queue}")
    await query.message.reply_text(
        f"Готово! Черга <b>{queue}</b>. Напишу, щойно для неї з'явиться або зміниться "
        "графік відключень.\n\n/my — графік на сьогодні й завтра\n"
        "/start — додати ще адресу\n/stop — відписатися",
        parse_mode="HTML")


async def cmd_my(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    queues = store.user_queues(uid)
    if not queues:
        await update.effective_message.reply_text(
            "Ви ще не підписані на жодну чергу.", reply_markup=MENU)
        return
    now = today()
    days = [date.fromordinal(now.toordinal() + i).isoformat() for i in (0, 1)]
    lines = []
    for day in days:
        known = [(q, store.get_slots(day, q)) for q in queues]
        known = [(q, s) for q, s in known if s is not None]
        if known:
            lines.append(f"<b>{human_day(day).capitalize()}</b>")
            lines += [f"Черга {q}: {fmt_slots(s)}" for q, s in known]
    await update.effective_message.reply_text(
        "\n".join(lines) or "Графіків на сьогодні й завтра ще не публікували.",
        parse_mode="HTML")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    store.unsubscribe_all(update.effective_user.id)
    await update.effective_message.reply_text("Відписав від усіх черг. Повернутися — /start.")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"Підписників: {store.subscriber_count()}")


# ---------- опитування джерела ----------
async def send_safe(bot, chat_id, text):
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        await bot.send_message(chat_id, text, parse_mode="HTML")
    except Forbidden:  # користувач заблокував бота
        store.unsubscribe_all(chat_id)
    except TelegramError:  # одна невдала відправка не має зупиняти розсилку
        log.exception("Не вдалося надіслати в %s", chat_id)


async def poll(ctx: ContextTypes.DEFAULT_TYPE):
    try:
        schedule = await source.fetch()
    except Exception:
        log.exception("Не вдалося отримати графік")
        return

    bot = ctx.bot
    for day in sorted(schedule):
        if day < today().isoformat():
            continue
        changed = []
        for q, slots in schedule[day].items():
            prev = store.get_slots(day, q)
            if not store.update_snapshot(day, q, slots):
                continue
            # Новий день без відключень з'являється щодня — про це мовчимо.
            if prev is None and not slots:
                continue
            changed.append(q)
        if not changed:
            continue
        log.info("%s: змінились черги %s", day, changed)

        for q in changed:
            text = (f"⚡️ <b>Черга {q}, {human_day(day)}</b>\n"
                    f"{fmt_slots(schedule[day][q])}")
            for uid in store.subscribers(q):
                await send_safe(bot, uid, text)
                await asyncio.sleep(0.05)  # ліміти Telegram на розсилку

        if CHANNEL_ID:
            await send_safe(bot, CHANNEL_ID, await channel_text(bot, day, schedule[day]))


async def channel_text(bot, day: str, queues: dict) -> str:
    me = await bot.get_me()
    lines = [f"⚡️ <b>Графік відключень, {human_day(day)}</b>", ""]
    lines += [f"{q}: {fmt_slots(queues.get(q))}" for q in QUEUES if q in queues]
    lines += ["", f"Сповіщення лише для вашої черги: @{me.username}"]
    return "\n".join(lines)


async def cmd_testpost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Тестовий пост у канал. Доступний лише адмінам каналу."""
    msg = update.effective_message
    if not CHANNEL_ID:
        await msg.reply_text("CHANNEL_ID не задано.")
        return
    try:
        member = await ctx.bot.get_chat_member(CHANNEL_ID, update.effective_user.id)
    except TelegramError as e:
        await msg.reply_text(f"Не бачу канал: {e.message}. Бот має бути адміном каналу.")
        return
    if member.status not in ("creator", "administrator"):
        await msg.reply_text("Команда лише для адмінів каналу.")
        return

    day = today().isoformat()
    queues = {q: store.get_slots(day, q) for q in QUEUES}
    queues = {q: s for q, s in queues.items() if s is not None}
    if not queues:
        await msg.reply_text("Графіка на сьогодні ще немає в базі.")
        return
    text = "🧪 <i>Тестовий пост</i>\n\n" + await channel_text(ctx.bot, day, queues)
    try:
        await ctx.bot.send_message(CHANNEL_ID, text, parse_mode="HTML")
    except TelegramError as e:
        await msg.reply_text(f"Канал відхилив пост: {e.message}")
        return
    await msg.reply_text("Надіслав тестовий пост у канал.")


async def post_init(app: Application):
    try:  # меню команд у Telegram; без нього бот теж працює
        await app.bot.set_my_commands([
            BotCommand("start", "Знайти чергу за адресою"),
            BotCommand("my", "Мій графік на сьогодні й завтра"),
            BotCommand("stop", "Відписатися від усіх черг"),
        ])
    except TelegramError:
        log.exception("Не вдалося задати меню команд")
    # Перший запуск: запам'ятати поточний стан мовчки, без розсилки.
    if store.is_empty():
        try:
            for day, queues in (await source.fetch()).items():
                for q, slots in queues.items():
                    store.update_snapshot(day, q, slots)
            log.info("Початковий стан збережено без розсилки")
        except Exception:
            log.exception("Початкове завантаження не вдалося")


def main():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("my", cmd_my))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("testpost", cmd_testpost))
    app.add_handler(CallbackQueryHandler(on_queue, pattern=r"^q:"))
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(on_street, pattern=r"^s:"))
    app.add_handler(CallbackQueryHandler(on_subscribe, pattern=r"^sub:"))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(poll, interval=POLL_MINUTES * 60, first=10)
    app.run_polling()


if __name__ == "__main__":
    main()
