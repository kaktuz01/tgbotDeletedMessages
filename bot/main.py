import asyncio
import logging
import os
import time
from pathlib import Path

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    BusinessConnection,
    BusinessMessagesDeleted,
    ErrorEvent,
    FSInputFile,
    Message,
)
from dotenv import load_dotenv

from .backup import backup_loop
from .billing import ADMINS, TRIAL_DAYS, describe, status
from .db import DB
from .safe import safe
from .ui import quick_ignore_kb, sub_kb
from .ui import register_middlewares
from .ui import router as ui_router
from .util import esc, fmt_ts, full_name, quote

load_dotenv()
log = logging.getLogger("bot")
router = Router()
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "data/media"))

MEDIA = {
    "photo": "send_photo",
    "video": "send_video",
    "voice": "send_voice",
    "video_note": "send_video_note",
    "audio": "send_audio",
    "animation": "send_animation",
    "sticker": "send_sticker",
    "document": "send_document",
}
DEFAULT_EXT = {
    "video": ".mp4", "voice": ".ogg", "video_note": ".mp4",
    "audio": ".mp3", "animation": ".mp4", "sticker": ".webp",
}


DOWNLOAD_LIMIT = 20 * 1024 * 1024  # Bot API cannot download bigger files
_downloads = asyncio.Semaphore(3)
_bg_tasks: set[asyncio.Task] = set()


def extract_media(msg: Message):
    """(kind, file_id, extension, size) of the attached media, or Nones."""
    if msg.photo:
        p = msg.photo[-1]
        return "photo", p.file_id, ".jpg", p.file_size
    for kind in ("video", "voice", "video_note", "audio", "animation", "sticker", "document"):
        obj = getattr(msg, kind)
        if obj:
            name = getattr(obj, "file_name", None)
            ext = Path(name).suffix if name else DEFAULT_EXT.get(kind, "")
            return kind, obj.file_id, ext, getattr(obj, "file_size", None)
    return None, None, None, None


async def download_media(bot: Bot, db: DB, key: tuple, file_id: str, dest: Path):
    """Background download; never blocks saving the message itself."""
    async with _downloads:
        for attempt in range(3):
            try:
                await bot.download(file_id, destination=dest, timeout=120)
                await db.set_media_path(*key, str(dest))
                return
            except TelegramBadRequest as e:  # too big / expired: retrying is pointless
                log.warning("media download skipped: %s", e)
                return
            except Exception as e:
                log.warning("media download failed (attempt %s/3): %s", attempt + 1, e)
                await asyncio.sleep(2 ** attempt)


async def store(bot: Bot, db: DB, msg: Message):
    kind, file_id, ext, size = extract_media(msg)
    cid, chat_id, mid = msg.business_connection_id, msg.chat.id, msg.message_id
    old = await db.get_message(cid, chat_id, mid) if file_id else None
    # the message goes to the DB FIRST: a quick deletion must always find it
    await db.save_message(
        connection_id=cid,
        chat_id=chat_id,
        message_id=mid,
        chat_name=msg.chat.full_name or msg.chat.title or str(chat_id),
        from_id=msg.from_user.id if msg.from_user else None,
        from_name=full_name(msg.from_user),
        date=int(msg.date.timestamp()),
        text=msg.text or msg.caption,
        media_kind=kind,
        media_path=None,
        media_file_id=file_id,
    )
    if not file_id:
        return
    if size and size > DOWNLOAD_LIMIT:
        log.info("media too big to download (%s bytes); will resend by file_id", size)
        return
    if old and old["media_file_id"] == file_id and old["media_path"] and Path(old["media_path"]).exists():
        return  # already downloaded (e.g. text edit of a media message)
    dest = MEDIA_DIR / str(chat_id) / f"{mid}{ext}"
    dest.parent.mkdir(parents=True, exist_ok=True)
    task = asyncio.create_task(download_media(bot, db, (cid, chat_id, mid), file_id, dest))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def get_conn(bot: Bot, db: DB, cid: str):
    """Connection row; fetched from Telegram if the connect event was missed."""
    conn = await db.get_connection(cid)
    if conn is None:
        bc = await safe(bot.get_business_connection, cid)
        if bc is None:
            return None
        await db.upsert_connection(bc.id, bc.user.id, bc.user_chat_id, bc.is_enabled)
        conn = await db.get_connection(cid)
    return conn if conn and conn["is_enabled"] else None


# ---------- business updates ----------

@router.business_connection()
async def on_connection(conn: BusinessConnection, bot: Bot, db: DB):
    await db.upsert_connection(conn.id, conn.user.id, conn.user_chat_id, conn.is_enabled)
    if conn.is_enabled:
        await db.start_trial(conn.user.id, TRIAL_DAYS, int(time.time()))
        _, kind, until = await status(db, conn.user.id)
        text = (
            "✅ <b>Бот подключён!</b>\n"
            "Теперь я сохраняю переписку и пришлю всё, что удалят или изменят.\n\n"
            f"{describe(kind, until)}\n"
            "Меню, история и настройки — /start"
        )
    else:
        text = "⛔ Бот отключён от вашего аккаунта."
    await safe(bot.send_message, conn.user_chat_id, text)


async def nudge(bot: Bot, db: DB, conn, hidden: int):
    """Tell an expired user (at most once a day) that something was deleted but is locked."""
    uid, now = conn["user_id"], int(time.time())
    a = await db.get_access(uid)
    if a is not None and now - a["last_nudge"] < 86400:
        return
    await db.touch_nudge(uid, now)
    await safe(
        bot.send_message,
        conn["user_chat_id"],
        f"🔒 <b>Удалено сообщений: {hidden}</b>\n"
        "Доступ закончился, поэтому показать их я не могу, но всё сохранено. "
        "Оформите подписку, и уведомления вернутся.",
        reply_markup=sub_kb("expired"),
    )


@router.business_message()
async def on_business_message(msg: Message, bot: Bot, db: DB):
    conn = await get_conn(bot, db, msg.business_connection_id)
    if conn is None or await db.is_ignored(conn["user_id"], msg.chat.id):
        return
    await store(bot, db, msg)


@router.edited_business_message()
async def on_edited(msg: Message, bot: Bot, db: DB):
    cid = msg.business_connection_id
    conn = await get_conn(bot, db, cid)
    if conn is None or await db.is_ignored(conn["user_id"], msg.chat.id):
        return
    old = await db.get_message(cid, msg.chat.id, msg.message_id)
    new_text = msg.text or msg.caption
    if old is not None and old["text"] != new_text:
        await db.mark_edited(cid, msg.chat.id, msg.message_id, old["text"], int(msg.date.timestamp()))
        s = await db.get_settings(conn["user_id"])
        own = old["from_id"] == conn["user_id"]
        if s["notify_edited"] and (not own or s["notify_own"]):
            if not (await status(db, conn["user_id"]))[0]:
                await nudge(bot, db, conn, 1)
            else:
                await safe(
                    bot.send_message,
                    conn["user_chat_id"],
                    f"✏️ <b>Сообщение изменено</b>\n"
                    f"👤 {esc(old['from_name'])}\n🕒 {fmt_ts(old['date'])}\n\n"
                    f"<b>Было:</b>\n{quote(old['text'], 1500)}\n"
                    f"<b>Стало:</b>\n{quote(new_text, 1500)}",
                    reply_markup=quick_ignore_kb(msg.chat.id, False),
                )
    await store(bot, db, msg)


async def notify_deleted(bot: Bot, owner: int, row):
    header = (
        f"🗑 <b>Сообщение удалено</b>\n"
        f"💬 Чат: {esc(row['chat_name'])}\n"
        f"✍️ Автор: {esc(row['from_name'])}\n"
        f"🕒 Отправлено: {fmt_ts(row['date'])}"
    )
    markup = quick_ignore_kb(row["chat_id"], False)
    kind, path = row["media_kind"], row["media_path"]
    sender = MEDIA.get(kind)
    # prefer the local copy; if it is not downloaded (yet), resend by Telegram file_id
    if path and Path(path).exists():
        src = FSInputFile(path)
    else:
        src = row["media_file_id"]
    if sender and src:
        if kind in ("video_note", "sticker"):
            await safe(bot.send_message, owner, header, reply_markup=markup)
            if await safe(getattr(bot, sender), owner, src) is None:
                await safe(bot.send_message, owner, "⚠️ Не удалось отправить файл.")
            return
        caption = header + (f"\n\n{quote(row['text'], 700)}" if row["text"] else "")
        if await safe(getattr(bot, sender), owner, src, caption=caption, reply_markup=markup):
            return
    body = f"\n\n{quote(row['text'], 3000)}" if row["text"] else f"\n\n<i>[{esc(kind or 'нет текста')}]</i>"
    await safe(bot.send_message, owner, header + body, reply_markup=markup)


@router.deleted_business_messages()
async def on_deleted(event: BusinessMessagesDeleted, bot: Bot, db: DB):
    cid = event.business_connection_id
    # Telegram does not say WHO deleted the messages, so log everything we know
    log.info("deleted_business_messages chat=%s ids=%s", event.chat.id, list(event.message_ids))
    conn = await get_conn(bot, db, cid)
    if conn is None:
        log.info("  skipped: unknown or disabled business connection")
        return
    if await db.is_ignored(conn["user_id"], event.chat.id):
        log.info("  skipped: chat is ignored")
        return
    await db.mark_deleted(cid, event.chat.id, event.message_ids)
    s = await db.get_settings(conn["user_id"])
    if not s["notify_deleted"]:
        log.info("  skipped: deletion notifications are off")
        return
    active = (await status(db, conn["user_id"]))[0]
    missing = hidden = 0
    found = []
    for mid in event.message_ids:
        try:
            row = await db.get_message(cid, event.chat.id, mid)
            if row is None:
                missing += 1
                continue
            found.append((mid, row["from_id"] == conn["user_id"]))
            if row["from_id"] != conn["user_id"] or s["notify_own"]:
                if active:
                    await notify_deleted(bot, conn["user_chat_id"], row)
                else:
                    hidden += 1
        except Exception:
            log.exception("failed to notify about deleted message %s", mid)
    log.info(
        "  in db: %s (id, own)=%s; not in db: %s; shown_notification=%s",
        len(found), found, missing, bool(active and s["notify_unknown"]) if missing else "n/a",
    )
    if hidden:
        await nudge(bot, db, conn, hidden)
    if missing and active and s["notify_unknown"]:
        await safe(
            bot.send_message,
            conn["user_chat_id"],
            f"🗑 <b>Удалено сообщений: {missing}</b>\n"
            f"💬 В чате с {esc(event.chat.full_name)}\n"
            "<i>Кто их удалил, Telegram не сообщает. В базе бота их нет: они могли прийти "
            "до подключения, пока бот был выключен или чат не входил в список доступных боту.</i>",
            reply_markup=quick_ignore_kb(event.chat.id, False),
        )


async def on_error(event: ErrorEvent):
    log.error("Unhandled error in handler", exc_info=event.exception)
    return True


async def main():
    logging.basicConfig(level=logging.INFO)
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Задайте BOT_TOKEN в .env")
    db_path = os.getenv("DB_PATH", "data/bot.db")
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    db = DB(db_path)
    await db.open()
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(db=db)
    dp.include_router(router)
    dp.include_router(ui_router)
    dp.errors.register(on_error)
    register_middlewares(dp)
    await safe(
        bot.set_my_commands,
        [
            BotCommand(command="start", description="Меню"),
            BotCommand(command="paysupport", description="Поддержка по оплате"),
        ],
    )
    for admin in ADMINS:
        await safe(
            bot.set_my_commands,
            [
                BotCommand(command="start", description="Меню"),
                BotCommand(command="addfriend", description="Добавить друга: /addfriend @ник"),
                BotCommand(command="delfriend", description="Снять доступ: /delfriend @ник"),
                BotCommand(command="friends", description="Список друзей"),
                BotCommand(command="paysupport", description="Поддержка по оплате"),
            ],
            scope=BotCommandScopeChat(chat_id=admin),
        )
    task = asyncio.create_task(backup_loop(bot, db))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
