import logging
import re
import time

from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .backup import send_backup
from .billing import ADMINS, PLAN_DAYS, PRICE_STARS, TRIAL_DAYS, classify, describe, status
from .db import DB
from .logsetup import only_problems, read_tail
from .safe import safe
from .util import esc, fmt_date, fmt_short, fmt_ts

log = logging.getLogger("bot.ui")
router = Router()
PAGE = 8

CONTACT_URL = "https://t.me/kaktuz_01"
LINE = "━━━━━━━━━━━━━━━━"

START_TEXT = (
    "🕵️ <b>Ничего не пропадёт</b>\n"
    f"{LINE}\n"
    "Я сохраняю переписку из ваших личных чатов и присылаю то, что собеседник "
    "удалил или изменил.\n\n"
    "🗑 удалённые сообщения и медиа\n"
    "✏️ правки: было → стало\n"
    "📚 история чатов файлом\n"
    "📦 резервные копии\n"
    f"{LINE}\n"
    "{status}\n"
    f"{LINE}\n"
    "Выберите действие 👇"
)
HELP_TEXT = (
    "🆘 <b>Помощь</b>\n"
    f"{LINE}\n"
    "<b>Как подключить</b>\n"
    "1️⃣ Настройки Telegram → <i>Telegram для бизнеса</i> → <i>Чат-боты</i>\n"
    "2️⃣ Выберите этого бота\n"
    "3️⃣ Включите право <i>читать сообщения</i>\n"
    "После этого бот напишет «Бот подключён». Нужен Telegram Premium.\n\n"
    "<b>Что важно знать</b>\n"
    "• бот видит только то, что пришло после подключения\n"
    "• работает в личных чатах (группы и секретные чаты недоступны)\n"
    "• файлы больше 20 МБ и таймерные медиа сохранить нельзя\n"
    "• 🔕 игнорируемый чат не сохраняется и без уведомлений\n\n"
    f"<b>Оплата</b>\nПервые {TRIAL_DAYS} дня бесплатно, дальше подписка в ⭐ Telegram Stars. "
    "После окончания срока бот продолжает сохранять переписку, но уведомления и выгрузка "
    "закрыты до оплаты. Проблемы с оплатой — /paysupport\n\n"
    "Вопросы и предложения — пишите автору 👇"
)
SETTING_LABELS = {
    "notify_deleted": "Уведомлять об удалённых",
    "notify_edited": "Уведомлять об изменённых",
    "notify_own": "Также про мои сообщения",
    "notify_unknown": "Удаления, которых нет в базе",
    "backup_daily": "Ежедневный бэкап базы",
}


def main_kb(user_id: int | None = None) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Чаты", callback_data="c:0")
    kb.button(text="⚙️ Настройки", callback_data="s")
    kb.button(text="📦 Бэкап", callback_data="bk")
    kb.button(text="🆘 Помощь", callback_data="h")
    kb.button(text="💎 Подписка", callback_data="sub")
    layout = [2, 2, 1]
    if user_id in ADMINS:
        kb.button(text="👑 Админ", callback_data="adm")
        layout.append(1)
    kb.adjust(*layout)
    return kb.as_markup()


def help_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="✉️ Написать автору", url=CONTACT_URL)
    kb.button(text="🏠 Меню", callback_data="m")
    kb.adjust(1)
    return kb.as_markup()


def quick_ignore_kb(chat_id: int, ignored: bool) -> InlineKeyboardMarkup:
    text = "🔔 Снова следить за чатом" if ignored else "🔕 Игнорировать этот чат"
    return InlineKeyboardMarkup(
        inline_keyboard=[[Btn(text=text, callback_data=f"qi:{chat_id}:{0 if ignored else 1}")]]
    )


async def show(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup, alert: str | None = None):
    if isinstance(cb.message, Message):
        await safe(cb.message.edit_text, text, reply_markup=kb)
    await safe(cb.answer, alert)


async def start_text(db: DB, user_id: int) -> str:
    _, kind, until = await status(db, user_id)
    return START_TEXT.replace("{status}", describe(kind, until))


@router.message(CommandStart())
async def start(msg: Message, db: DB):
    await safe(msg.answer, await start_text(db, msg.from_user.id), reply_markup=main_kb(msg.from_user.id))


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await safe(cb.answer)


@router.callback_query(F.data == "m")
async def menu(cb: CallbackQuery, db: DB):
    await show(cb, await start_text(db, cb.from_user.id), main_kb(cb.from_user.id))


@router.callback_query(F.data == "h")
async def help_view(cb: CallbackQuery):
    await show(cb, HELP_TEXT, help_kb())


# ---------- chats ----------

@router.callback_query(F.data.startswith("c:"))
async def chats(cb: CallbackQuery, db: DB):
    rows = await db.chats(cb.from_user.id)
    kb = InlineKeyboardBuilder()
    if not rows:
        kb.button(text="🏠 Меню", callback_data="m")
        await show(cb, "Пока ничего не сохранено. Проверьте подключение бота в настройках бизнеса.", kb.as_markup())
        return
    pages = (len(rows) + PAGE - 1) // PAGE
    page = min(max(int(cb.data[2:]), 0), pages - 1)
    for r in rows[page * PAGE:(page + 1) * PAGE]:
        name = (r["chat_name"] or str(r["chat_id"]))[:24]
        extra = f" · 🗑{r['d']}" if r["d"] else ""
        kb.button(text=f"{name} · {r['n']}{extra}", callback_data=f"chat:{r['chat_id']}")
    kb.adjust(1)
    nav = []
    if page > 0:
        nav.append(Btn(text="◀", callback_data=f"c:{page - 1}"))
    nav.append(Btn(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(Btn(text="▶", callback_data=f"c:{page + 1}"))
    kb.row(*nav)
    kb.row(Btn(text="🏠 Меню", callback_data="m"))
    await show(cb, f"💬 <b>Сохранённые чаты</b>\n{LINE}\nВыберите чат 👇", kb.as_markup())


async def render_chat(cb: CallbackQuery, db: DB, chat_id: int, alert: str | None = None):
    uid = cb.from_user.id
    st = await db.chat_stats(uid, chat_id)
    name = await db.chat_name(uid, chat_id)
    ignored = await db.is_ignored(uid, chat_id)
    kb = InlineKeyboardBuilder()
    kb.button(text="📄 Последние 100", callback_data=f"x:{chat_id}:100")
    kb.button(text="📚 Вся история", callback_data=f"x:{chat_id}:all")
    kb.button(text="🗑 Только удалённые", callback_data=f"x:{chat_id}:del")
    kb.button(
        text="🔔 Снова следить" if ignored else "🔕 Игнорировать чат",
        callback_data=f"ci:{chat_id}:{0 if ignored else 1}",
    )
    kb.button(text="◀ К чатам", callback_data="c:0")
    kb.adjust(1)
    text = (
        f"👤 <b>{esc(name)}</b>\n{LINE}\n"
        f"💬 Сообщений: <b>{st['n']}</b>\n🗑 Удалено: <b>{st['d']}</b>\n✏️ Изменено: <b>{st['e']}</b>"
        + ("\n\n🔕 <i>Чат игнорируется: не сохраняется и без уведомлений</i>" if ignored else "")
    )
    await show(cb, text, kb.as_markup(), alert)


@router.callback_query(F.data.startswith("chat:"))
async def chat_view(cb: CallbackQuery, db: DB):
    await render_chat(cb, db, int(cb.data[5:]))


@router.callback_query(F.data.startswith("ci:"))
async def chat_ignore(cb: CallbackQuery, db: DB):
    _, cid, flag = cb.data.split(":")
    uid, chat_id = cb.from_user.id, int(cid)
    name = await db.chat_name(uid, chat_id)
    await db.set_ignored(uid, chat_id, name, flag == "1")
    await render_chat(cb, db, chat_id, "Чат игнорируется" if flag == "1" else "Слежка включена")


@router.callback_query(F.data.startswith("qi:"))
async def quick_ignore(cb: CallbackQuery, db: DB):
    """Button under a deletion notification."""
    _, cid, flag = cb.data.split(":")
    uid, chat_id = cb.from_user.id, int(cid)
    name = await db.chat_name(uid, chat_id)
    ignored = flag == "1"
    await db.set_ignored(uid, chat_id, name, ignored)
    if isinstance(cb.message, Message):
        await safe(cb.message.edit_reply_markup, reply_markup=quick_ignore_kb(chat_id, ignored))
    await safe(cb.answer, "Чат игнорируется" if ignored else "Слежка включена")


@router.callback_query(F.data.startswith("x:"))
async def export(cb: CallbackQuery, db: DB):
    if not (await status(db, cb.from_user.id))[0]:
        await render_sub(cb, db, "🔒 Выгрузка доступна по подписке")
        return
    _, cid, mode = cb.data.split(":")
    chat_id = int(cid)
    rows = await db.history(
        cb.from_user.id, chat_id, 100 if mode == "100" else None, deleted_only=mode == "del"
    )
    if not rows:
        await safe(cb.answer, "Ничего не найдено", show_alert=True)
        return
    out = []
    for r in rows:
        flags = (" [удалено]" if r["deleted"] else "") + (" [изменено]" if r["edited"] else "")
        content = r["text"] or ""
        if r["media_kind"]:
            content = f"[{r['media_kind']}] {content}".strip()
        out.append(f"[{fmt_ts(r['date'])}] {r['from_name']}{flags}: {content}")
    if isinstance(cb.message, Message):
        await safe(
            cb.message.answer_document,
            BufferedInputFile("\n".join(out).encode(), filename=f"chat_{chat_id}_{mode}.txt"),
        )
    await safe(cb.answer)


# ---------- settings ----------

async def render_settings(cb: CallbackQuery, db: DB, alert: str | None = None):
    uid = cb.from_user.id
    s = await db.get_settings(uid)
    kb = InlineKeyboardBuilder()
    for field, label in SETTING_LABELS.items():
        kb.button(text=f"{'✅' if s[field] else '❌'} {label}", callback_data=f"t:{field}")
    n = len(await db.ignored_list(uid))
    kb.button(text=f"🚫 Игнорируемые чаты ({n})", callback_data="il")
    kb.button(text="🏠 Меню", callback_data="m")
    kb.adjust(1)
    await show(
        cb,
        f"⚙️ <b>Настройки</b>\n{LINE}\nНажмите на пункт, чтобы включить или выключить.\n"
        "📦 Бэкап приходит файлом раз в сутки и содержит только ваши данные.",
        kb.as_markup(),
        alert,
    )


@router.callback_query(F.data == "s")
async def settings(cb: CallbackQuery, db: DB):
    await render_settings(cb, db)


@router.callback_query(F.data.startswith("t:"))
async def toggle(cb: CallbackQuery, db: DB):
    try:
        await db.toggle_setting(cb.from_user.id, cb.data[2:])
    except ValueError:
        await safe(cb.answer)
        return
    await render_settings(cb, db)


async def render_ignored(cb: CallbackQuery, db: DB):
    rows = await db.ignored_list(cb.from_user.id)
    kb = InlineKeyboardBuilder()
    for r in rows:
        kb.button(text=f"🔔 {(r['chat_name'] or str(r['chat_id']))[:30]}", callback_data=f"iu:{r['chat_id']}")
    kb.button(text="◀ Настройки", callback_data="s")
    kb.adjust(1)
    text = (
        f"🚫 <b>Игнорируемые чаты</b>\n{LINE}\nНажмите на чат, чтобы снова следить за ним:"
        if rows
        else "Игнорируемых чатов нет."
    )
    await show(cb, text, kb.as_markup())


@router.callback_query(F.data == "il")
async def ignored_view(cb: CallbackQuery, db: DB):
    await render_ignored(cb, db)


@router.callback_query(F.data.startswith("iu:"))
async def unignore(cb: CallbackQuery, db: DB):
    await db.set_ignored(cb.from_user.id, int(cb.data[3:]), None, False)
    await render_ignored(cb, db)


@router.callback_query(F.data == "bk")
async def backup_now(cb: CallbackQuery, bot: Bot, db: DB):
    if not (await status(db, cb.from_user.id))[0]:
        await render_sub(cb, db, "🔒 Бэкап доступен по подписке")
        return
    await safe(cb.answer, "Готовлю бэкап…")
    ok = await send_backup(bot, db, cb.from_user.id)
    if not ok and isinstance(cb.message, Message):
        await safe(cb.message.answer, "⚠️ Не удалось создать бэкап. Подробности в логах бота.")


# ---------- subscription (Telegram Stars) ----------

def sub_kb(kind: str = "none") -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    if kind not in ("admin", "free"):
        kb.button(
            text=f"⭐ {'Продлить' if kind == 'paid' else 'Оплатить'} на {PLAN_DAYS} дн. · {PRICE_STARS} ⭐",
            callback_data="pay",
        )
    kb.button(text="🏠 Меню", callback_data="m")
    kb.adjust(1)
    return kb.as_markup()


async def render_sub(cb: CallbackQuery, db: DB, alert: str | None = None):
    active, kind, until = await status(db, cb.from_user.id)
    text = (
        f"💎 <b>Подписка</b>\n{LINE}\n{describe(kind, until)}\n\n"
        f"Доступ на {PLAN_DAYS} дн.: <b>{PRICE_STARS} ⭐</b>\n"
        "Оплата прямо в Telegram, дни добавляются к текущему сроку.\n\n"
        "Без подписки бот продолжает сохранять переписку, но уведомления "
        "и выгрузка закрыты до оплаты.\n\n"
        f"🆔 Ваш ID: <code>{cb.from_user.id}</code>"
    )
    await show(cb, text, sub_kb(kind), alert)


@router.callback_query(F.data == "sub")
async def sub_view(cb: CallbackQuery, db: DB):
    await render_sub(cb, db)


@router.callback_query(F.data == "pay")
async def pay(cb: CallbackQuery, bot: Bot):
    await safe(
        bot.send_invoice,
        cb.from_user.id,
        title=f"Доступ на {PLAN_DAYS} дн.",
        description="Уведомления об удалённых и изменённых сообщениях, история и бэкапы.",
        payload=f"sub:{PLAN_DAYS}",
        currency="XTR",
        prices=[LabeledPrice(label=f"{PLAN_DAYS} дн.", amount=PRICE_STARS)],
    )
    await safe(cb.answer)


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    ok = q.invoice_payload.startswith("sub:") and q.currency == "XTR"
    await safe(q.answer, ok=ok, error_message=None if ok else "Неизвестный товар")


@router.message(F.successful_payment)
async def paid(msg: Message, db: DB):
    import time

    sp = msg.successful_payment
    until = await db.add_paid(
        msg.from_user.id, PLAN_DAYS, sp.telegram_payment_charge_id, sp.total_amount, int(time.time())
    )
    if until is None:  # duplicate delivery of the same payment
        return
    await safe(
        msg.answer,
        f"✅ <b>Оплата получена, спасибо!</b>\n{describe('paid', until)}",
        reply_markup=main_kb(msg.from_user.id),
    )


@router.message(Command("paysupport"))
async def paysupport(msg: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="✉️ Написать автору", url=CONTACT_URL)
    await safe(
        msg.answer,
        "💬 <b>Поддержка по оплате</b>\nЕсли списали звёзды, а доступ не появился, "
        "или нужен возврат — напишите автору, укажите свой ID и время платежа.",
        reply_markup=kb.as_markup(),
    )


# ---------- admin: friends with free access ----------

async def render_admin(cb: CallbackQuery, db: DB):
    import time

    st = await db.stats(int(time.time()))
    friends = await db.free_list()
    kb = InlineKeyboardBuilder()
    kb.button(text="👥 Пользователи", callback_data="ul:0")
    kb.button(text="📜 Логи: 300 строк", callback_data="lg:300")
    kb.button(text="⚠️ Логи: только ошибки", callback_data="lg:err")
    for f in friends:
        kb.button(text=f"❌ {(f['name'] or str(f['user_id']))[:30]}", callback_data=f"fd:{f['user_id']}")
    kb.button(text="🏠 Меню", callback_data="m")
    kb.adjust(1)
    text = (
        f"👑 <b>Админ</b>\n{LINE}\n"
        f"👥 Пользователей: <b>{st['users']}</b>\n"
        f"🎁 На пробном периоде: <b>{st['trial']}</b>\n"
        f"💎 С активной подпиской: <b>{st['paid']}</b>\n"
        f"🤝 Друзей (бесплатно): <b>{st['friends']}</b>\n"
        f"⭐ Получено звёзд: <b>{st['stars']}</b> ({st['payments']} платежей)\n"
        + "\n<b>Друзья</b> (бесплатный доступ)"
        + ("\nНажмите на друга, чтобы снять доступ:" if friends else "\nПока никого.")
        + "\n\nКоманды: /addfriend @ник · /delfriend @ник · /friends"
    )
    await show(cb, text, kb.as_markup())


@router.callback_query(F.data == "adm")
async def admin_view(cb: CallbackQuery, db: DB):
    if cb.from_user.id not in ADMINS:
        await safe(cb.answer, "Недостаточно прав", show_alert=True)
        return
    await render_admin(cb, db)


USERS_PAGE = 8


def _short_status(user_id: int, row: dict, now: int) -> str:
    _, kind, until = classify(user_id, row["free"], row["paid_until"], row["trial_until"], now)
    d = fmt_date(until) if until else ""
    return {
        "admin": "👑 админ",
        "free": "🤝 друг",
        "paid": f"💎 до {d}",
        "trial": f"🎁 пробный до {d}",
        "expired": f"⏳ истёк {d}",
        "none": "➖ без доступа",
    }[kind]


@router.callback_query(F.data.startswith("ul:"))
async def users_list(cb: CallbackQuery, db: DB):
    import time

    if cb.from_user.id not in ADMINS:
        await safe(cb.answer, "Недостаточно прав", show_alert=True)
        return
    rows = await db.users_overview()
    pages = max(1, (len(rows) + USERS_PAGE - 1) // USERS_PAGE)
    try:
        page = min(max(int(cb.data[3:]), 0), pages - 1)
    except ValueError:
        page = 0
    now = int(time.time())
    lines = []
    for i, r in enumerate(rows[page * USERS_PAGE:(page + 1) * USERS_PAGE], page * USERS_PAGE + 1):
        who = esc(r["name"] or "без имени") + (f" (@{esc(r['username'])})" if r["username"] else "")
        lines.append(
            f"<b>{i}.</b> {who}\n"
            f"    🆔 <code>{r['user_id']}</code> · {_short_status(r['user_id'], r, now)}\n"
            f"    {'🔌 подключён' if r['connected'] else '🔌 не подключён'} · был(а): {fmt_short(r['last_seen'])}"
        )
    kb = InlineKeyboardBuilder()
    nav = []
    if page > 0:
        nav.append(Btn(text="◀", callback_data=f"ul:{page - 1}"))
    nav.append(Btn(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(Btn(text="▶", callback_data=f"ul:{page + 1}"))
    kb.row(*nav)
    kb.row(Btn(text="◀ Админ", callback_data="adm"))
    connected = sum(1 for r in rows if r["connected"])
    text = (
        f"👥 <b>Пользователи бота</b>: {len(rows)} · подключили: {connected}\n{LINE}\n"
        + ("\n\n".join(lines) if lines else "Пока никого.")
    )
    await show(cb, text, kb.as_markup())


@router.callback_query(F.data.startswith("lg:"))
async def send_logs(cb: CallbackQuery):
    if cb.from_user.id not in ADMINS:  # admin-only, checked again on every press
        await safe(cb.answer, "Недостаточно прав", show_alert=True)
        return
    mode = cb.data[3:]
    if mode == "err":
        lines, name = only_problems(read_tail(3000))[-300:], "bot_errors.txt"
    elif mode in ("100", "300", "1000"):
        lines, name = read_tail(int(mode)), f"bot_log_{mode}.txt"
    else:
        await safe(cb.answer)
        return
    if not lines or not isinstance(cb.message, Message):
        await safe(cb.answer, "В логе пока ничего нет" if not lines else None, show_alert=bool(not lines))
        return
    await safe(cb.message.answer_document, BufferedInputFile("\n".join(lines).encode(), filename=name))
    await safe(cb.answer)


@router.callback_query(F.data.startswith("fd:"))
async def friend_delete(cb: CallbackQuery, db: DB):
    if cb.from_user.id not in ADMINS:
        await safe(cb.answer, "Недостаточно прав", show_alert=True)
        return
    await db.set_free(int(cb.data[3:]), False)
    await render_admin(cb, db)


USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{4,32}$")


def _usernames(command: CommandObject) -> list[str]:
    return [t.lstrip("@") for t in (command.args or "").split() if USERNAME_RE.match(t.lstrip("@"))]


def _is_admin(msg: Message) -> bool:
    return bool(msg.from_user) and msg.from_user.id in ADMINS


@router.message(Command("addfriend"))
async def add_friend(msg: Message, command: CommandObject, bot: Bot, db: DB):
    if not _is_admin(msg):
        return
    names = _usernames(command)
    if not names:
        await safe(msg.answer, "Укажите ник: <code>/addfriend @username</code> (можно несколько через пробел)")
        return
    lines = []
    for u in names:
        uid = await db.user_id_by_username(u)
        if uid:
            await db.set_free(uid, True)
            lines.append(f"✅ @{esc(u)}: бесплатный доступ включён")
            await safe(bot.send_message, uid, "🤝 <b>Автор выдал вам бесплатный доступ!</b>",
                       reply_markup=main_kb(uid))
        else:
            await db.add_pending(u)
            lines.append(f"⏳ @{esc(u)}: бот его ещё не видел. Доступ включится сам, когда он нажмёт /start или подключит бота")
    await safe(msg.answer, "\n".join(lines))


@router.message(Command("delfriend"))
async def del_friend(msg: Message, command: CommandObject, db: DB):
    if not _is_admin(msg):
        return
    names = _usernames(command)
    if not names:
        await safe(msg.answer, "Укажите ник: <code>/delfriend @username</code>")
        return
    lines = []
    for u in names:
        uid = await db.user_id_by_username(u)
        removed = await db.del_pending(u)
        if uid:
            await db.set_free(uid, False)
            removed = True
        lines.append(f"{'🗑' if removed else '❔'} @{esc(u)}: {'доступ снят' if removed else 'в списке не найден'}")
    await safe(msg.answer, "\n".join(lines))


@router.message(Command("friends"))
async def friends(msg: Message, db: DB):
    if not _is_admin(msg):
        return
    rows, pending = await db.free_list(), await db.pending_list()
    lines = [f"🤝 {esc(r['name'] or r['user_id'])}" + (f" (@{esc(r['username'])})" if r["username"] else "") for r in rows]
    lines += [f"⏳ @{esc(u)} (ждёт первого входа)" for u in pending]
    await safe(msg.answer, "<b>Друзья с бесплатным доступом</b>\n" + ("\n".join(lines) if lines else "Пока никого."))


# ---------- tracking users (needed to resolve @username) ----------

class UserTracker(BaseMiddleware):
    EVENTS = {"message", "callback_query", "business_connection", "pre_checkout_query"}

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and not user.is_bot and getattr(event, "event_type", None) in self.EVENTS:
            db: DB = data["db"]
            if await db.touch_user(user.id, user.username, user.full_name, int(time.time())):
                await safe(
                    data["bot"].send_message, user.id,
                    "🤝 <b>Автор выдал вам бесплатный доступ!</b>", reply_markup=main_kb(user.id),
                )
        return await handler(event, data)


def register_middlewares(dp) -> None:
    dp.update.outer_middleware(UserTracker())


# must stay the LAST message handler: anything else typed to the bot opens the menu
@router.message(F.text)
async def fallback(msg: Message, db: DB):
    await start(msg, db)
