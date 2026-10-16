"""Бот графіків відключень Житомирщини (графік один на всю область, черги 1.1–6.2).

Користувач знаходить свою чергу (1.1–6.2) за адресою і отримує повідомлення,
коли графік черги з'являється або змінюється, а також нагадування за 30 хв до
вимкнення й у момент увімкнення. У канал іде картинка графіка при кожній зміні
і пост «О 14:00 вимикаємо / вмикаємо» перед кожним перемиканням.
"""
import asyncio
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from telegram import (BotCommand, InlineKeyboardButton, InlineKeyboardMarkup,
                      InputMediaPhoto, KeyboardButton, ReplyKeyboardMarkup, ReplyParameters,
                      Update)
from telegram.error import Forbidden, RetryAfter, TelegramError
from telegram.ext import (Application, ApplicationHandlerStop, CallbackQueryHandler,
                          CommandHandler, ContextTypes, MessageHandler, filters)

import address
import card
import posts
from source import make_source
from storage import Store

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # інакше в лог потрапляє URL з токеном
log = logging.getLogger("svitlo")

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = os.getenv("CHANNEL_ID") or None
POLL_MINUTES = int(os.getenv("POLL_MINUTES", "10"))
CHANNEL_LEAD = 15   # пост «О 14:00 вимикаємо…» у канал — за стільки хвилин
REMIND_LEAD = 30    # особисте «Через 30 хв вимкнуть світло»
GRACE = 5           # після рестарту бота не надсилати події, що минули давніше

QUEUES = posts.QUEUES

KYIV = ZoneInfo("Europe/Kyiv")


def today() -> date:
    """Сервер живе в UTC, а графіки — за київським часом."""
    return datetime.now(KYIV).date()


def light_mode() -> bool:
    """Легкий режим (за замовчуванням): графік обленерго подаємо як «можливі відключення»,
    без постів перемикань і нагадувань — поки їх фактично не застосовують. Жорсткий
    вмикає адмін у боті, коли відключення реальні."""
    return store.get_meta("mode") != "hard"


SOURCE = os.getenv("SOURCE", "demo")
store = Store(os.getenv("DB_PATH", "svitlo.db"))
source = make_source(SOURCE)
if store.bind_source(SOURCE):
    log.info("Джерело змінилось на %s: старі знімки стерто", SOURCE)


# ---------- клавіатури ----------
def queues_keyboard(user_id: int) -> InlineKeyboardMarkup:
    mine = set(store.user_queues(user_id))
    buttons = [InlineKeyboardButton(("✅ " if q in mine else "") + q,
                                    callback_data=f"q:{q}") for q in QUEUES]
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
    return InlineKeyboardMarkup(rows)


def reminders_keyboard(user_id: int) -> InlineKeyboardMarkup:
    on = store.reminders_on(user_id)
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "🔕 Вимкнути нагадування" if on else "🔔 Увімкнути нагадування за 30 хв",
        callback_data="rem:off" if on else "rem:on")]])


BTN_FIND, BTN_MY, BTN_QUEUES = "🔎 Знайти чергу за адресою", "📅 Мій графік", "🔢 Мої черги"
BTN_REMIND, BTN_HELP = "⏰ Нагадування", "❓ Допомога"
BTN_POST, BTN_CARD, BTN_MODE = "📢 Новий пост", "🖼 Графік у канал", "⚙️ Режим"
MENU_BUTTONS = {BTN_FIND, BTN_MY, BTN_QUEUES, BTN_REMIND, BTN_HELP, BTN_POST, BTN_CARD, BTN_MODE}


def main_keyboard(admin: bool) -> ReplyKeyboardMarkup:
    rows = [[BTN_FIND], [BTN_MY, BTN_QUEUES], [BTN_REMIND, BTN_HELP]]
    if admin:
        rows += [[BTN_POST, BTN_CARD], [BTN_MODE]]
    return ReplyKeyboardMarkup([[KeyboardButton(b) for b in r] for r in rows],
                               resize_keyboard=True, is_persistent=True)


HELP = ("<b>Як це працює</b>\n\n"
        "1. Знайдіть свою чергу: напишіть адресу — <i>Київська 24</i> у Житомирі, "
        "<i>Бердичів, Житомирська 12</i> чи просто назву села. Або оберіть чергу вручну.\n"
        "2. Я напишу, коли для вашої черги з'явиться або зміниться графік.\n"
        "3. Коли відключення реально застосовують — нагадаю за 30 хв до вимкнення "
        "і повідомлю, коли світло має повернутись. Поки графіки діють «про всяк випадок», "
        "пишу не частіше разу на день і без звуку.\n\n"
        "Графік один для всієї області. Можна стежити за кількома адресами: дім, робота, батьки.\n\n"
        "Дані — з сайту Житомиробленерго, перевіряю кожні 10 хвилин.")


MENU = InlineKeyboardMarkup([
    [InlineKeyboardButton("🔎 Знайти чергу за адресою", callback_data="menu:addr")],
    [InlineKeyboardButton("Я знаю свою чергу", callback_data="menu:pick")],
])
ASK_ADDRESS = ("Напишіть адресу на Житомирщині:\n"
               "• у Житомирі — вулицю й будинок: <i>Київська 24</i>\n"
               "• в іншому місті чи селі — через кому: <i>Бердичів, Житомирська 12</i>\n"
               "• або просто назву села: <i>Березівка</i>")
SITE_DOWN = ("Сайт Житомиробленерго зараз не відповідає, тож адресу перевірити не можу. "
             "Спробуйте пізніше або оберіть чергу вручну.")


# ---------- команди ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    mine = store.user_queues(update.effective_user.id)
    text = ("Я надсилаю графік відключень світла на Житомирщині лише для вашої черги — "
            "і лише коли він з'являється або змінюється. Без зайвих повідомлень.\n\n")
    text += (f"Ваші черги: <b>{', '.join(mine)}</b>. Додати ще одну адресу?"
             if mine else "Спершу знайдемо вашу чергу. Можна просто написати адресу.")
    admin = await is_channel_admin(ctx.bot, update.effective_user.id)
    text += "\n\nКнопки меню — внизу 👇"
    await update.effective_message.reply_text(text, parse_mode="HTML",
                                              reply_markup=main_keyboard(admin))


async def on_menu_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Кнопки постійного меню внизу чату."""
    msg, uid = update.effective_message, update.effective_user.id
    ctx.user_data.pop("compose", None)
    btn = msg.text
    if btn == BTN_FIND:
        ctx.user_data.clear()
        await msg.reply_text(ASK_ADDRESS, parse_mode="HTML")
    elif btn == BTN_MY:
        await cmd_my(update, ctx)
    elif btn == BTN_QUEUES:
        await msg.reply_text("Ваші черги позначені ✅. Натисніть, щоб додати або прибрати.",
                             reply_markup=queues_keyboard(uid))
    elif btn == BTN_REMIND and light_mode():
        await msg.reply_text(
            "⏰ Зараз нагадування на паузі для всіх: графіки обленерго застосовують не завжди, "
            "і ми не хочемо турбувати даремно. Щойно відключення стануть реальними — "
            "нагадуватиму за 30 хв до вимкнення.\n\nВаше налаштування:",
            reply_markup=reminders_keyboard(uid))
    elif btn == BTN_REMIND:
        on = store.reminders_on(uid)
        await msg.reply_text(
            "⏰ Нагадування за 30 хв до вимкнення і в момент увімкнення: "
            f"<b>{'увімкнено' if on else 'вимкнено'}</b>.\n"
            "Про зміни графіка я пишу завжди.", parse_mode="HTML",
            reply_markup=reminders_keyboard(uid))
    elif btn == BTN_HELP:
        await msg.reply_text(HELP, parse_mode="HTML", reply_markup=MENU)
    elif btn == BTN_POST:
        await start_compose(update, ctx)
    elif btn == BTN_CARD:
        await cmd_preview(update, ctx)
    elif btn == BTN_MODE:
        await show_mode(update, ctx)


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


# ---------- пошук черги за адресою (вся область) ----------
HOUSE = re.compile(r"\d+[\w/\-]*")


def _place(ctx) -> tuple[str, str]:
    return ctx.user_data.get("place") or address.ZHYTOMYR


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Будь-який текст у приваті — це адреса.

    «Київська 24» — вулиця в Житомирі; «Бердичів, Житомирська 12» — інший населений
    пункт; «Березівка» — село (покажемо чергу або спитаємо вулицю); «24» — будинок.
    """
    msg = update.effective_message
    text = (msg.text or "").strip()
    try:
        await address.ensure_zhytomyr()
        if ctx.user_data.get("street") and HOUSE.fullmatch(text):
            await show_house(msg, ctx, ctx.user_data["street"], text)
            return
        place_q, rest = address.split_place(text)
        if place_q and not HOUSE.fullmatch(rest):  # «Київська, 24» — це вулиця з будинком
            found, _ = await address.search_places(place_q)
            if not found:
                await msg.reply_text(
                    f"Не знайшов населений пункт «{place_q}» на Житомирщині. "
                    "Перевірте назву, наприклад: <i>Бердичів, Житомирська 12</i>.",
                    parse_mode="HTML", reply_markup=MENU)
                return
            ctx.user_data["pending"] = rest
            await choose_place(msg, ctx, found)
            return

        query, house = address.split_address(text.replace(",", " "))
        found = await address.search_streets(query, *_place(ctx))
        # Без номера будинку це може бути назва села чи міста — навіть посеред пошуку в іншому.
        places, exact = ([], False) if house else await address.search_places(query)
        if exact or (not found and places):
            ctx.user_data.pop("street", None)
            await choose_place(msg, ctx, places)
            return
    except (httpx.HTTPError, ValueError):
        log.exception("Пошук адреси на сайті обленерго не вдався")
        await msg.reply_text(SITE_DOWN, reply_markup=MENU)
        return

    if not found:
        where = (await address.find_place(*_place(ctx)) or {}).get("name", "населеному пункті")
        await msg.reply_text(
            f"Не знайшов вулицю «{query}» ({where}). Спробуйте лише назву, без «вул.».\n\n"
            "Інше місто чи село — напишіть так: <i>Бердичів, Житомирська 12</i>, "
            "або просто назву села.", parse_mode="HTML", reply_markup=MENU)
        return
    ctx.user_data["house"] = house
    await choose_street(msg, ctx, found)


async def choose_place(msg, ctx, places: list[dict]):
    if len(places) == 1:
        await pick_place(msg, ctx, places[0])
        return
    buttons = [[InlineKeyboardButton(address.place_label(p), callback_data=f"c:{p['rem']}:{p['id']}")]
               for p in places]
    await msg.reply_text("Який саме населений пункт?", reply_markup=InlineKeyboardMarkup(buttons))


async def choose_street(msg, ctx, found: list[tuple[str, str]]):
    rem, cid = _place(ctx)
    if len(found) == 1:
        await pick_street(msg, ctx, found[0][0])
        return
    buttons = [[InlineKeyboardButton(name, callback_data=f"s:{rem}:{cid}:{sid}")] for sid, name in found]
    await msg.reply_text("Яка саме вулиця?", reply_markup=InlineKeyboardMarkup(buttons))


async def on_place(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, rem, cid = query.data.split(":")
    try:
        await pick_place(query.message, ctx, await address.find_place(rem, cid))
    except (httpx.HTTPError, ValueError):
        log.exception("Пошук адреси на сайті обленерго не вдався")
        await query.message.reply_text(SITE_DOWN, reply_markup=MENU)


async def pick_place(msg, ctx, p: dict):
    rem, cid = p["rem"], p["id"]
    ctx.user_data["place"] = (rem, cid)
    ctx.user_data.pop("street", None)
    label = address.place_label(p)
    pending = ctx.user_data.pop("pending", None)
    if pending:  # «Бердичів, Житомирська 12» — вулицю вже написали
        query, house = address.split_address(pending)
        found = await address.search_streets(query, rem, cid)
        if found:
            ctx.user_data["house"] = house
            await choose_street(msg, ctx, found)
            return
        await msg.reply_text(f"У {label} не знайшов вулицю «{query}». Напишіть лише назву вулиці.")
        return

    records = await address.place_records(rem, cid)
    again = InlineKeyboardButton("🔎 Інша адреса", callback_data="menu:addr")
    if records is not None:
        queues = address.place_queues(records)
        if not queues:
            await msg.reply_text(f"📍 {label}\nЦей населений пункт не відключають за графіками — "
                                 "лише у разі аварій.", reply_markup=InlineKeyboardMarkup([[again]]))
            return
        if len(queues) == 1 and all(r["queue"] == queues[0] for r in records):
            ctx.user_data.pop("place", None)
            await msg.reply_text(
                f"📍 {label}\nЧерга для всього населеного пункту: <b>{queues[0]}</b>",
                parse_mode="HTML", reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton(f"✅ Підписатися на {queues[0]}", callback_data=f"sub:{queues[0]}")],
                     [again]]))
            return
    streets = await address.streets(rem, cid)
    if 0 < len(streets) <= 12:
        buttons = [[InlineKeyboardButton(name, callback_data=f"s:{rem}:{cid}:{sid}")] for sid, name in streets]
        await msg.reply_text(f"📍 {label}\nЯка вулиця?", reply_markup=InlineKeyboardMarkup(buttons))
    else:
        await msg.reply_text(f"📍 {label}\nНапишіть вулицю і номер будинку, наприклад: "
                             "<i>Шевченка 5</i>", parse_mode="HTML")


async def on_street(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    if len(parts) == 4:  # s:rem:cid:street
        ctx.user_data["place"] = (parts[1], parts[2])
    else:                # старі кнопки s:street — Житомир
        ctx.user_data.pop("place", None)
    try:
        await address.ensure_zhytomyr()
        await pick_street(query.message, ctx, parts[-1])
    except (httpx.HTTPError, ValueError):
        log.exception("Пошук адреси на сайті обленерго не вдався")
        await query.message.reply_text(SITE_DOWN, reply_markup=MENU)


async def pick_street(msg, ctx, street_id: str):
    ctx.user_data["street"] = street_id
    house = ctx.user_data.pop("house", None)
    if house:
        await show_house(msg, ctx, street_id, house)
    else:
        name = await address.street_name(*_place(ctx), street_id)
        await msg.reply_text(f"{name}. Який номер будинку?")


async def address_label(ctx, street: str, house: str) -> str:
    rem, cid = _place(ctx)
    if (rem, cid) == address.ZHYTOMYR:
        return f"{street}, {house}"
    p = await address.find_place(rem, cid)
    return f"{p['name'] if p else ''}, {street}, {house}"


async def show_house(msg, ctx, street_id: str, house: str):
    rem, cid = _place(ctx)
    name = await address.street_name(rem, cid, street_id)
    records = await address.street_records(rem, cid, street_id)
    hits = address.find_house(records, house)
    again = InlineKeyboardButton("🔎 Інша адреса", callback_data="menu:addr")
    label = await address_label(ctx, name, house)

    if not hits:
        queues = sorted({r["queue"] for r in records if r["queue"]})
        buttons = [[InlineKeyboardButton(f"Підписатися на {q}", callback_data=f"sub:{q}")]
                   for q in queues]
        where = await address_label(ctx, name, house)
        await msg.reply_text(
            f"Будинку немає в списку обленерго: {where}. Напишіть інший номер"
            + (f" або оберіть чергу — на цій вулиці є: {', '.join(queues)}." if queues else ".")
            + "\n\nІнше місто чи село — «🔎 Інша адреса».",
            reply_markup=InlineKeyboardMarkup(buttons + [[again]]))
        return

    queues = list(dict.fromkeys(r["queue"] for r in hits if r["queue"]))
    if not queues:
        await msg.reply_text(
            f"📍 {label}\nЦей будинок не відключають за графіками — "
            "лише у разі аварій. Підписуватися не потрібно.",
            reply_markup=InlineKeyboardMarkup([[again]]))
        return
    for key in ("street", "place"):
        ctx.user_data.pop(key, None)
    lines = [f"📍 {label}"]
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
        f"Готово! Черга <b>{queue}</b>.\n\n"
        "• Напишу, щойно для неї з'явиться або зміниться графік\n"
        "• Коли відключення реально застосовують — нагадаю за 30 хв до вимкнення\n\n"
        "/my — графік на сьогодні й завтра\n/start — додати ще адресу\n/stop — відписатися",
        parse_mode="HTML", reply_markup=reminders_keyboard(uid))


async def on_reminders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    on = query.data == "rem:on"
    store.set_reminders(query.from_user.id, on)
    await query.answer("Нагадування увімкнено" if on else
                       "Нагадування вимкнено. Про зміни графіка писатиму й надалі.", show_alert=not on)
    await query.edit_message_reply_markup(reminders_keyboard(query.from_user.id))


async def cmd_my(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    queues = store.user_queues(uid)
    if not queues:
        await update.effective_message.reply_text(
            "Ви ще не підписані на жодну чергу.", reply_markup=MENU)
        return
    t = today()
    blocks = [posts.personal_day(q, d, t, store.get_slots(d.isoformat(), q))
              for q in queues for d in (t, posts.tomorrow(t))]
    if light_mode():
        blocks.append(posts.LIGHT_NOTE + "\n⏰ Нагадування зараз на паузі для всіх.")
    else:
        status = "увімкнено" if store.reminders_on(uid) else "вимкнено"
        blocks.append(f"⏰ Нагадування за 30 хв до вимкнення: {status}")
    await update.effective_message.reply_text(
        "\n\n".join(blocks), parse_mode="HTML", reply_markup=reminders_keyboard(uid))


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    store.unsubscribe_all(update.effective_user.id)
    await update.effective_message.reply_text("Відписав від усіх черг. Повернутися — /start.")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(f"Підписників: {store.subscriber_count()}")


# ---------- опитування джерела ----------
async def send_safe(bot, chat_id, text, silent: bool = False):
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML", disable_notification=silent)
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        await bot.send_message(chat_id, text, parse_mode="HTML", disable_notification=silent)
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

    bot, t = ctx.bot, today()
    for day, command in getattr(source, "commands", {}).items():  # обсяг обмежень Укренерго
        store.set_meta(f"cmd:{day}", json.dumps(command, ensure_ascii=False))
    for day in sorted(schedule):
        if day < t.isoformat():
            continue
        before = store.get_day(day)
        changed = []
        for q, slots in schedule[day].items():
            prev = before.get(q)
            if not store.update_snapshot(day, q, slots):
                continue
            # Новий день без відключень з'являється щодня — про це мовчимо.
            if prev is None and not slots:
                continue
            changed.append(q)
        if not changed:
            # День з відключеннями, по якому ще не було картки (напр. графік з'явився до
            # оновлення бота) — публікуємо один раз, без «оновлено».
            if CHANNEL_ID and any(schedule[day].values()) and not store.get_meta(f"post:{day}"):
                await publish_day(bot, CHANNEL_ID, date.fromisoformat(day), schedule[day],
                                  light=light_mode())
            continue
        log.info("%s: змінились черги %s", day, changed)

        d, light = date.fromisoformat(day), light_mode()
        for q in changed:
            slots = schedule[day][q]
            if light:
                # Одне тихе повідомлення на чергу за день; подальші зміни — лише в /my.
                if not slots or not store.mark_sent(f"pday:{day}:{q}", day):
                    continue
                text, silent = posts.personal_light(q, d, t, slots), True
            else:
                text, silent = posts.personal_change(q, d, t, slots, before.get(q)), False
            for uid in store.subscribers(q):
                await send_safe(bot, uid, text, silent=silent)
                await asyncio.sleep(0.05)  # ліміти Telegram на розсилку

        if CHANNEL_ID:
            first = not any(before.values())  # раніше цього дня відключень не було
            await publish_day(bot, CHANNEL_ID, d, schedule[day],
                              changed=None if first else set(changed),
                              cancelled=not first and not any(schedule[day].values()),
                              light=light)


def channel_button(bot) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "🔔 Сповіщення для моєї черги", url=f"https://t.me/{bot.username}?start=channel")]])


async def publish_day(bot, chat_id, d: date, queues: dict, *, changed: set | None = None,
                      cancelled: bool = False, prefix: str = "", remember: bool = True,
                      light: bool | None = None):
    """Картинка графіка + підпис.

    Жорсткий режим: оновлення — новим постом відповіддю на перший пост дня (зі звуком).
    Легкий режим: оновлення редагує той самий пост — без нового повідомлення в каналі.
    """
    t = today()
    light = light_mode() if light is None else light
    updated = datetime.now(KYIV).strftime("%H:%M") if changed or cancelled else None
    png = await asyncio.to_thread(  # Pillow — синхронний; не блокуємо цикл подій
        card.render, posts.day_title(d, t), posts.card_subtitle(d, queues, light), queues,
        updated=updated, changed=changed or set(),
        footer=f"Сповіщення для вашої черги → @{bot.username}", light=light)
    command = store.get_meta(f"cmd:{d.isoformat()}")
    caption = prefix + posts.channel_caption(d, t, queues, bot.username, changed=changed,
                                             cancelled=cancelled,
                                             command=json.loads(command) if command else None,
                                             light=light, updated=updated)
    prev = store.get_meta(f"post:{d.isoformat()}") if remember else None
    if light and prev:
        try:
            return await bot.edit_message_media(
                InputMediaPhoto(png, caption=caption, parse_mode="HTML"),
                chat_id=chat_id, message_id=int(prev), reply_markup=channel_button(bot))
        except TelegramError:
            log.exception("Не вдалося відредагувати пост %s — публікую новий", prev)
            prev = None
    try:
        msg = await bot.send_photo(
            chat_id, png, caption=caption, parse_mode="HTML", reply_markup=channel_button(bot),
            reply_parameters=ReplyParameters(int(prev), allow_sending_without_reply=True)
            if prev else None)
    except TelegramError:
        log.exception("Не вдалося опублікувати графік у %s", chat_id)
        return None
    if remember:
        store.set_meta(f"post:{d.isoformat()}", str(msg.message_id))
    return msg


# ---------- перемикання за графіком: пости в канал і особисті нагадування ----------
async def tick(ctx: ContextTypes.DEFAULT_TYPE):
    now_dt = datetime.now(KYIV)
    t, now = now_dt.date(), now_dt.hour * 60 + now_dt.minute
    if now_dt.hour == 4 and now_dt.minute == 0:
        store.cleanup((t - timedelta(days=2)).isoformat())
    if light_mode():  # легкий режим: без постів перемикань і нагадувань
        return
    td, tm = store.get_day(t.isoformat()), store.get_day(posts.tomorrow(t).isoformat())
    if not any(td.values()) and not any(tm.values()):
        return
    tl = posts.timeline(td, tm)
    for at, event in sorted(posts.switch_events(tl).items()):
        day = (t + timedelta(days=at // 1440)).isoformat()
        key = f"{day}@{posts.hhmm(at)}"

        if CHANNEL_ID and at - CHANNEL_LEAD <= now < at + GRACE and store.mark_sent(f"ch:{key}", day):
            post_id = store.get_meta(f"post:{day}")
            try:
                await ctx.bot.send_message(
                    CHANNEL_ID, posts.switch_post(at, event, tl), parse_mode="HTML",
                    disable_notification=True,  # щопівгодини — без звуку, картки графіка зі звуком
                    reply_markup=channel_button(ctx.bot),
                    reply_parameters=ReplyParameters(int(post_id), allow_sending_without_reply=True)
                    if post_id else None)
            except TelegramError:
                log.exception("Не вдалося надіслати пост про перемикання")

        for q, end in event["off"].items():
            if at - REMIND_LEAD <= now < at + GRACE and store.mark_sent(f"off:{key}:{q}", day):
                await remind(ctx.bot, q, posts.reminder_off(q, at, end, now))
        for q in event["on"]:
            if at <= now < at + GRACE and store.mark_sent(f"on:{key}:{q}", day):
                nxt = next((s for s, _ in tl[q] if s > at), None)
                await remind(ctx.bot, q, posts.reminder_on(q, at, nxt))


async def warm_places(ctx: ContextTypes.DEFAULT_TYPE):
    """Список ~1760 населених пунктів — 20 запитів до сайту; тягнемо заздалегідь, не під час діалогу."""
    try:
        log.info("Населених пунктів у пошуку: %d", len(await address.settlements()))
    except (httpx.HTTPError, ValueError):
        log.exception("Не вдалося завантажити населені пункти")


async def remind(bot, queue: str, text: str):
    for uid in store.subscribers(queue):
        if store.reminders_on(uid):
            await send_safe(bot, uid, text)
            await asyncio.sleep(0.05)


# ---------- для адмінів каналу ----------
async def is_channel_admin(bot, user_id: int) -> bool:
    if not CHANNEL_ID:
        return False
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
    except TelegramError:
        return False
    return member.status in ("creator", "administrator")


async def channel_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    msg = update.effective_message
    if not CHANNEL_ID:
        await msg.reply_text("CHANNEL_ID не задано.")
        return False
    try:
        member = await ctx.bot.get_chat_member(CHANNEL_ID, update.effective_user.id)
    except TelegramError as e:
        await msg.reply_text(f"Не бачу канал: {e.message}. Бот має бути адміном каналу.")
        return False
    if member.status not in ("creator", "administrator"):
        await msg.reply_text("Команда лише для адмінів каналу.")
        return False
    return True


def preview_day() -> tuple[date, dict, bool]:
    """Найближчий день з відключеннями з бази, а якщо їх немає — демо-графік на завтра."""
    t = today()
    for d in (posts.tomorrow(t), t):
        queues = store.get_day(d.isoformat())
        if any(queues.values()):
            return d, queues, False
    return posts.tomorrow(t), posts.DEMO, True


async def cmd_preview(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Показує адміну в особистих, як виглядатимуть пости. У канал нічого не йде."""
    if not await channel_admin(update, ctx):
        return
    d, queues, demo = preview_day()
    note = "🧪 <i>Прев'ю на демо-даних — реальних відключень зараз немає</i>\n\n" if demo else \
           "🧪 <i>Прев'ю</i>\n\n"
    chat = update.effective_chat.id
    await publish_day(ctx.bot, chat, d, queues, prefix=note, remember=False)
    await publish_day(ctx.bot, chat, d, queues, changed={"3.2", "5.1"} if demo else None,
                      prefix=note, remember=False)
    tl = posts.timeline({}, queues) if d != today() else posts.timeline(queues, {})
    events = posts.switch_events(tl)
    if light_mode():
        q = next((q for q in posts.QUEUES if queues.get(q)), None)
        if q:
            await ctx.bot.send_message(chat, note + posts.personal_light(q, d, today(), queues[q]),
                                       parse_mode="HTML")
        await ctx.bot.send_message(chat, "🟡 Зараз легкий режим: оновлення редагують той самий пост, "
                                         "постів «О 14:00 вимикаємо» і нагадувань немає. Змінити — «⚙️ Режим».")
    elif events:
        at = min(events, key=lambda a: (-len(events[a]["off"]) - len(events[a]["on"]), a))
        await ctx.bot.send_message(chat, note + posts.switch_post(at, events[at], tl), parse_mode="HTML")
        q, end = next(iter(events[at]["off"].items()), (None, None))
        if q:
            await ctx.bot.send_message(chat, note + posts.reminder_off(q, at, end, at - 30),
                                       parse_mode="HTML", reply_markup=reminders_keyboard(chat))
    if demo:
        await ctx.bot.send_message(chat, "Реальних відключень у базі немає — публікувати нічого. "
                                         "Ручний пост: «📢 Новий пост».")
        return
    await ctx.bot.send_message(
        chat, f"Опублікувати графік {posts.day_inline(d, today())} у канал?\n"
              "Піде перша картка (без «оновлено»), як у прев'ю.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Опублікувати", callback_data=f"card:{d.isoformat()}"),
            InlineKeyboardButton("❌ Ні", callback_data="card:no")]]))


MODE_TEXT = {
    True: ("🟡 <b>Легкий режим</b>\n"
           "• Канал: одна картка на день «Графік можливих відключень», оновлення редагують той самий пост\n"
           "• Постів «О 14:00 вимикаємо / вмикаємо» немає\n"
           "• Бот: одне тихе повідомлення на чергу за день, без нагадувань\n"
           "• Скрізь приписка: графіки застосовують не завжди"),
    False: ("🔴 <b>Жорсткий режим</b>\n"
            "• Канал: картка «Графік відключень», оновлення — новим постом зі звуком\n"
            "• Пости «О 14:00 вимикаємо / вмикаємо» за 15 хв (без звуку)\n"
            "• Бот: кожна зміна графіка, нагадування за 30 хв і «світло має повернутись»"),
}


def mode_keyboard(light: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "🔴 Увімкнути жорсткий — відключення реальні" if light else "🟡 Повернути легкий режим",
        callback_data="mode:hard" if light else "mode:light")]])


async def show_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await channel_admin(update, ctx):
        return
    light = light_mode()
    await update.effective_message.reply_text(
        "Зараз: " + MODE_TEXT[light] + "\n\nДіє, поки не перемкнете.",
        parse_mode="HTML", reply_markup=mode_keyboard(light))


async def on_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not await channel_admin(update, ctx):
        return
    light = query.data == "mode:light"
    store.set_meta("mode", "light" if light else "hard")
    log.info("Режим змінено на %s адміном %s", "light" if light else "hard", query.from_user.id)
    await query.edit_message_reply_markup(None)
    await query.message.reply_text("✅ Увімкнено. " + MODE_TEXT[light], parse_mode="HTML",
                                   reply_markup=mode_keyboard(light))


async def on_card_publish(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_reply_markup(None)
    if query.data == "card:no" or not await channel_admin(update, ctx):
        return
    d = date.fromisoformat(query.data.split(":", 1)[1])
    queues = store.get_day(d.isoformat())
    msg = await publish_day(ctx.bot, CHANNEL_ID, d, queues, remember=False)
    await query.message.reply_text(f"✅ Опубліковано: {post_link(msg)}" if msg else
                                   "Канал відхилив пост — деталі в логах Railway.")


def post_link(msg) -> str:
    """Посилання на пост у каналі: t.me/<username>/N або t.me/c/<id>/N для закритого каналу."""
    chat = msg.chat
    if getattr(chat, "username", None):
        return f"https://t.me/{chat.username}/{msg.message_id}"
    return f"https://t.me/c/{str(chat.id).removeprefix('-100')}/{msg.message_id}"


# ---------- ручні пости в канал ----------
def compose_keyboard(with_button: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Опублікувати в канал", callback_data="post:pub")],
        [InlineKeyboardButton(("✅" if with_button else "⬜️") + " Кнопка «Сповіщення для моєї черги»",
                              callback_data="post:btn")],
        [InlineKeyboardButton("✏️ Інший варіант", callback_data="post:again"),
         InlineKeyboardButton("❌ Скасувати", callback_data="post:cancel")],
    ])


async def start_compose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await channel_admin(update, ctx):
        return
    ctx.user_data["compose"] = True
    await update.effective_message.reply_text(
        "📢 Надішліть пост одним повідомленням: текст, фото чи відео з підписом. "
        "Форматування (жирний, посилання, цитати) збережеться.\n\n"
        "Я покажу, як він виглядатиме в каналі, і попрошу підтвердити.")


async def on_compose(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Ловить чернетку поста, поки адмін у режимі «Новий пост». Інакше пропускає далі."""
    msg = update.effective_message
    if not ctx.user_data.get("compose") or msg.text in MENU_BUTTONS:
        return
    ctx.user_data["compose"] = False
    with_button = ctx.user_data.setdefault("post_btn", True)
    preview = await ctx.bot.copy_message(
        msg.chat_id, msg.chat_id, msg.message_id,
        reply_markup=channel_button(ctx.bot) if with_button else None)
    ctx.user_data["draft"] = (msg.chat_id, msg.message_id, preview.message_id)
    await msg.reply_text("👆 Так пост виглядатиме в каналі.",
                         reply_markup=compose_keyboard(with_button))
    raise ApplicationHandlerStop


async def on_post_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    action = query.data.split(":", 1)[1]
    draft = ctx.user_data.get("draft")
    if action in ("pub", "btn") and not draft:
        await query.answer("Чернетку не знайдено — почніть заново: «📢 Новий пост»", show_alert=True)
        return
    if action == "btn":
        with_button = not ctx.user_data.get("post_btn", True)
        ctx.user_data["post_btn"] = with_button
        chat_id, _, preview_id = draft
        await ctx.bot.edit_message_reply_markup(
            chat_id, preview_id, reply_markup=channel_button(ctx.bot) if with_button else None)
        await query.edit_message_reply_markup(compose_keyboard(with_button))
        await query.answer()
        return
    await query.answer()
    await query.edit_message_reply_markup(None)
    if action == "again":
        await start_compose(update, ctx)
    elif action == "cancel":
        ctx.user_data.pop("draft", None)
        await query.message.reply_text("Скасовано, у канал нічого не пішло.")
    elif action == "pub":
        if not await channel_admin(update, ctx):
            return
        chat_id, message_id, _ = draft
        try:
            sent = await ctx.bot.copy_message(
                CHANNEL_ID, chat_id, message_id,
                reply_markup=channel_button(ctx.bot) if ctx.user_data.get("post_btn", True) else None)
        except TelegramError as e:
            await query.message.reply_text(f"Канал відхилив пост: {e.message}")
            return
        ctx.user_data.pop("draft", None)
        link = f"https://t.me/c/{str(CHANNEL_ID).removeprefix('-100')}/{sent.message_id}"
        await query.message.reply_text(f"✅ Опубліковано: {link}")


async def cmd_testpost(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Тестовий пост у канал. Доступний лише адмінам каналу."""
    if not await channel_admin(update, ctx):
        return
    d, queues, demo = preview_day()
    note = "🧪 <i>Тестовий пост" + (" на демо-даних" if demo else "") + "</i>\n\n"
    await publish_day(ctx.bot, CHANNEL_ID, d, queues, prefix=note, remember=False)
    await update.effective_message.reply_text("Надіслав тестовий пост у канал.")


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
    app.add_handler(CommandHandler("preview", cmd_preview))
    app.add_handler(CommandHandler("post", start_compose))
    # Чернетка ручного поста перехоплюється раніше за пошук адреси (група -1).
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, on_compose), group=-1)
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.Text(list(MENU_BUTTONS)), on_menu_button))
    app.add_handler(CallbackQueryHandler(on_card_publish, pattern=r"^card:"))
    app.add_handler(CallbackQueryHandler(on_mode, pattern=r"^mode:"))
    app.add_handler(CommandHandler("mode", show_mode))
    app.add_handler(CallbackQueryHandler(on_post_action, pattern=r"^post:"))
    app.add_handler(CallbackQueryHandler(on_reminders, pattern=r"^rem:"))
    app.add_handler(CallbackQueryHandler(on_queue, pattern=r"^q:"))
    app.add_handler(CallbackQueryHandler(on_menu, pattern=r"^menu:"))
    app.add_handler(CallbackQueryHandler(on_street, pattern=r"^s:"))
    app.add_handler(CallbackQueryHandler(on_place, pattern=r"^c:"))
    app.add_handler(CallbackQueryHandler(on_subscribe, pattern=r"^sub:"))
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(poll, interval=POLL_MINUTES * 60, first=10)
    app.job_queue.run_repeating(tick, interval=60, first=30)
    app.job_queue.run_repeating(warm_places, interval=12 * 3600, first=20)
    app.run_polling()


if __name__ == "__main__":
    main()
