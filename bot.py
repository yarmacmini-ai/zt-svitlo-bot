"""Бот графіків відключень Житомира.

Користувач обирає свою чергу (1.1–6.2) і отримує повідомлення лише тоді,
коли графік його черги з'являється або змінюється. У канал іде зведення
по всіх чергах при кожній зміні.
"""
import asyncio
import logging
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes)

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


store = Store(os.getenv("DB_PATH", "svitlo.db"))
source = make_source(os.getenv("SOURCE", "demo"))


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


# ---------- команди ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Оберіть свою чергу — я напишу, щойно для неї з'явиться або зміниться "
        "графік відключень. Можна обрати кілька (дім, робота, батьки).\n\n"
        "Свою чергу можна дізнатися на сайті Житомиробленерго за адресою.",
        reply_markup=queues_keyboard(update.effective_user.id))


async def on_queue(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    queue = query.data.split(":", 1)[1]
    on = store.toggle(query.from_user.id, queue)
    await query.answer(f"Черга {queue}: {'підписано' if on else 'відписано'}")
    await query.edit_message_reply_markup(queues_keyboard(query.from_user.id))


async def cmd_my(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    queues = store.user_queues(uid)
    if not queues:
        await update.effective_message.reply_text("Ви ще не обрали черги. Натисніть /start.")
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
        changed = [q for q, slots in schedule[day].items()
                   if store.update_snapshot(day, q, slots)]
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
            me = await bot.get_me()
            lines = [f"⚡️ <b>Графік відключень, {human_day(day)}</b>", ""]
            lines += [f"{q}: {fmt_slots(schedule[day].get(q))}"
                      for q in QUEUES if q in schedule[day]]
            lines += ["", f"Сповіщення лише для вашої черги: @{me.username}"]
            await send_safe(bot, CHANNEL_ID, "\n".join(lines))


async def post_init(app: Application):
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
    app.add_handler(CallbackQueryHandler(on_queue, pattern=r"^q:"))
    app.job_queue.run_repeating(poll, interval=POLL_MINUTES * 60, first=10)
    app.run_polling()


if __name__ == "__main__":
    main()
