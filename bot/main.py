import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import (
    BotCommand,
    BusinessConnection,
    BusinessMessagesDeleted,
    ErrorEvent,
    FSInputFile,
    Message,
)
from dotenv import load_dotenv

from .backup import backup_loop
from .db import DB
from .safe import safe
from .ui import quick_ignore_kb
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


def extract_media(msg: Message):
    if msg.photo:
        return "photo", msg.photo[-1].file_id, ".jpg"
    for kind in ("video", "voice", "video_note", "audio", "animation", "sticker", "document"):
        obj = getattr(msg, kind)
        if obj:
            name = getattr(obj, "file_name", None)
            return kind, obj.file_id, (Path(name).suffix if name else DEFAULT_EXT.get(kind, ""))
    return None, None, None


async def store(bot: Bot, db: DB, msg: Message):
    kind, file_id, ext = extract_media(msg)
    path = None
    if file_id:
        dest = MEDIA_DIR / str(msg.chat.id) / f"{msg.message_id}{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            await bot.download(file_id, destination=dest)
            path = str(dest)
        except Exception as e:  # file > 20 MB, expired, network...
            log.warning("media download failed: %s", e)
    await db.save_message(
        connection_id=msg.business_connection_id,
        chat_id=msg.chat.id,
        message_id=msg.message_id,
        chat_name=msg.chat.full_name or msg.chat.title or str(msg.chat.id),
        from_id=msg.from_user.id if msg.from_user else None,
        from_name=full_name(msg.from_user),
        date=int(msg.date.timestamp()),
        text=msg.text or msg.caption,
        media_kind=kind,
        media_path=path,
    )


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
    text = (
        "✅ <b>Бот подключён!</b>\n"
        "Теперь я сохраняю переписку и пришлю всё, что удалят или изменят.\n\n"
        "Меню, история и настройки — /start"
        if conn.is_enabled
        else "⛔ Бот отключён от вашего аккаунта."
    )
    await safe(bot.send_message, conn.user_chat_id, text)


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
        f"👤 {esc(row['from_name'])}\n🕒 {fmt_ts(row['date'])}"
    )
    markup = quick_ignore_kb(row["chat_id"], False)
    kind, path = row["media_kind"], row["media_path"]
    sender = MEDIA.get(kind)
    if sender and path and Path(path).exists():
        if kind in ("video_note", "sticker"):
            await safe(bot.send_message, owner, header, reply_markup=markup)
            if await safe(getattr(bot, sender), owner, FSInputFile(path)) is None:
                await safe(bot.send_message, owner, "⚠️ Не удалось отправить файл.")
            return
        caption = header + (f"\n\n{quote(row['text'], 700)}" if row["text"] else "")
        if await safe(getattr(bot, sender), owner, FSInputFile(path), caption=caption, reply_markup=markup):
            return
    body = f"\n\n{quote(row['text'], 3000)}" if row["text"] else f"\n\n<i>[{esc(kind or 'нет текста')}]</i>"
    await safe(bot.send_message, owner, header + body, reply_markup=markup)


@router.deleted_business_messages()
async def on_deleted(event: BusinessMessagesDeleted, bot: Bot, db: DB):
    cid = event.business_connection_id
    conn = await get_conn(bot, db, cid)
    if conn is None or await db.is_ignored(conn["user_id"], event.chat.id):
        return
    await db.mark_deleted(cid, event.chat.id, event.message_ids)
    s = await db.get_settings(conn["user_id"])
    if not s["notify_deleted"]:
        return
    missing = 0
    for mid in event.message_ids:
        try:
            row = await db.get_message(cid, event.chat.id, mid)
            if row is None:
                missing += 1
            elif row["from_id"] != conn["user_id"] or s["notify_own"]:
                await notify_deleted(bot, conn["user_chat_id"], row)
        except Exception:
            log.exception("failed to notify about deleted message %s", mid)
    if missing:
        await safe(
            bot.send_message,
            conn["user_chat_id"],
            f"🗑 <b>Удалено сообщений: {missing}</b>\n"
            f"👤 {esc(event.chat.full_name)}\n"
            "<i>Бот не успел их сохранить: они пришли до подключения.</i>",
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
    await safe(bot.set_my_commands, [BotCommand(command="start", description="Меню")])
    task = asyncio.create_task(backup_loop(bot, db))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
