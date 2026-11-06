import logging

from aiogram import Bot, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton as Btn,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .backup import send_backup
from .db import DB
from .safe import safe
from .util import esc, fmt_ts

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
    "Вопросы и предложения — пишите автору 👇"
)
SETTING_LABELS = {
    "notify_deleted": "Уведомлять об удалённых",
    "notify_edited": "Уведомлять об изменённых",
    "notify_own": "Также про мои сообщения",
    "backup_daily": "Ежедневный бэкап базы",
}


def main_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="💬 Чаты", callback_data="c:0")
    kb.button(text="⚙️ Настройки", callback_data="s")
    kb.button(text="📦 Бэкап", callback_data="bk")
    kb.button(text="🆘 Помощь", callback_data="h")
    kb.adjust(2, 2)
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


@router.message(CommandStart())
@router.message(F.text)
async def start(msg: Message):
    await safe(msg.answer, START_TEXT, reply_markup=main_kb())


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await safe(cb.answer)


@router.callback_query(F.data == "m")
async def menu(cb: CallbackQuery):
    await show(cb, START_TEXT, main_kb())


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
    await safe(cb.answer, "Готовлю бэкап…")
    ok = await send_backup(bot, db, cb.from_user.id)
    if not ok and isinstance(cb.message, Message):
        await safe(cb.message.answer, "⚠️ Не удалось создать бэкап. Подробности в логах бота.")
