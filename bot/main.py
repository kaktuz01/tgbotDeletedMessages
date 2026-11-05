import asyncio
import html
import logging
import os
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BufferedInputFile,
    BusinessConnection,
    BusinessMessagesDeleted,
    FSInputFile,
    Message,
)
from dotenv import load_dotenv

from .db import DB

load_dotenv()
log = logging.getLogger("bot")
router = Router()
db: DB
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "data/media"))

# kind -> (attribute on Message, sender method name)
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


def esc(s) -> str:
    return html.escape(str(s or ""))


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M:%S")


def full_name(u) -> str:
    if not u:
        return "?"
    return (u.full_name or "") + (f" (@{u.username})" if u.username else "")


async def extract_media(bot: Bot, msg: Message):
    """Return (kind, file_id, ext) for the media in the message, if any."""
    if msg.photo:
        return "photo", msg.photo[-1].file_id, ".jpg"
    for kind in ("video", "voice", "video_note", "audio", "animation", "sticker", "document"):
        obj = getattr(msg, kind)
        if obj:
            name = getattr(obj, "file_name", None)
            ext = Path(name).suffix if name else {
                "video": ".mp4", "voice": ".ogg", "video_note": ".mp4",
                "audio": ".mp3", "animation": ".mp4", "sticker": ".webp",
            }.get(kind, "")
            return kind, obj.file_id, ext
    return None, None, None


async def store(bot: Bot, msg: Message):
    cid = msg.business_connection_id
    kind, file_id, ext = await extract_media(bot, msg)
    path = None
    if file_id:
        dest = MEDIA_DIR / str(msg.chat.id) / f"{msg.message_id}{ext}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            await bot.download(file_id, destination=dest)
            path = str(dest)
        except Exception as e:  # file too big (>20MB) etc.
            log.warning("media download failed: %s", e)
    await db.save_message(
        connection_id=cid,
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


async def owner_chat(cid: str):
    conn = await db.get_connection(cid)
    return conn["user_chat_id"] if conn and conn["is_enabled"] else None


# ---------- business updates ----------

@router.business_connection()
async def on_connection(conn: BusinessConnection, bot: Bot):
    await db.upsert_connection(conn.id, conn.user.id, conn.user_chat_id, conn.is_enabled)
    text = (
        "✅ Бот подключён. Начинаю сохранять переписку и удалённые сообщения."
        if conn.is_enabled
        else "⛔ Бот отключён от вашего аккаунта."
    )
    await bot.send_message(conn.user_chat_id, text)


@router.business_message()
async def on_business_message(msg: Message, bot: Bot):
    await store(bot, msg)


@router.edited_business_message()
async def on_edited(msg: Message, bot: Bot):
    cid = msg.business_connection_id
    old = await db.get_message(cid, msg.chat.id, msg.message_id)
    new_text = msg.text or msg.caption
    owner = await owner_chat(cid)
    if old is not None and owner and old["text"] != new_text:
        await db.mark_edited(cid, msg.chat.id, msg.message_id, old["text"], int(msg.date.timestamp()))
        await bot.send_message(
            owner,
            f"✏️ <b>{esc(old['chat_name'])}</b> — {esc(old['from_name'])} изменил(а) сообщение\n\n"
            f"<b>Было:</b>\n{esc(old['text'])}\n\n<b>Стало:</b>\n{esc(new_text)}",
        )
    await store(bot, msg)


@router.deleted_business_messages()
async def on_deleted(event: BusinessMessagesDeleted, bot: Bot):
    cid = event.business_connection_id
    owner = await owner_chat(cid)
    await db.mark_deleted(cid, event.chat.id, event.message_ids)
    if not owner:
        return
    for mid in event.message_ids:
        row = await db.get_message(cid, event.chat.id, mid)
        if row is None:
            await bot.send_message(
                owner, f"🗑 В чате <b>{esc(event.chat.full_name)}</b> удалено сообщение, "
                       "но бот его не успел сохранить."
            )
            continue
        header = (
            f"🗑 <b>{esc(row['chat_name'])}</b> — {esc(row['from_name'])} удалил(а) сообщение "
            f"от {fmt_ts(row['date'])}"
        )
        sender = MEDIA.get(row["media_kind"])
        path = row["media_path"]
        if sender and path and Path(path).exists():
            caption = header + (f"\n\n{esc(row['text'])}" if row["text"] else "")
            if row["media_kind"] in ("video_note", "sticker"):
                await bot.send_message(owner, header)
                await getattr(bot, sender)(owner, FSInputFile(path))
            else:
                await getattr(bot, sender)(owner, FSInputFile(path), caption=caption[:1024])
        else:
            body = f"\n\n{esc(row['text'])}" if row["text"] else f"\n\n[{row['media_kind'] or 'нет текста'}]"
            await bot.send_message(owner, header + body)


# ---------- commands in the private chat with the bot ----------

@router.message(CommandStart())
async def start(msg: Message):
    await msg.answer(
        "Привет! Я сохраняю удалённые и изменённые сообщения из ваших личных чатов.\n\n"
        "<b>Подключение:</b>\nНастройки Telegram → Telegram для бизнеса → Чат-боты → "
        "укажите этого бота и дайте доступ к чатам (с правом читать сообщения). "
        "Нужен Telegram Premium.\n\n"
        "<b>Команды:</b>\n"
        "/chats — список сохранённых чатов\n"
        "/history &lt;chat_id&gt; [N] — выгрузка последних N сообщений файлом\n"
        "/deleted &lt;chat_id&gt; — все удалённые сообщения чата"
    )


@router.message(Command("chats"))
async def chats(msg: Message):
    rows = await db.chats(msg.from_user.id)
    if not rows:
        await msg.answer("Пока ничего не сохранено. Проверьте подключение бота в настройках бизнеса.")
        return
    lines = [
        f"<code>{r['chat_id']}</code> — {esc(r['chat_name'])}: {r['n']} сообщ., удалено {r['d'] or 0}"
        for r in rows
    ]
    await msg.answer("\n".join(lines))


async def export(msg: Message, command: CommandObject, deleted_only: bool):
    args = (command.args or "").split()
    if not args or not args[0].lstrip("-").isdigit():
        await msg.answer("Укажите chat_id из /chats.")
        return
    limit = int(args[1]) if len(args) > 1 and args[1].isdigit() else None
    rows = await db.history(msg.from_user.id, int(args[0]), limit, deleted_only)
    if not rows:
        await msg.answer("Ничего не найдено.")
        return
    out = []
    for r in rows:
        flags = (" [удалено]" if r["deleted"] else "") + (" [изменено]" if r["edited"] else "")
        content = r["text"] or ""
        if r["media_kind"]:
            content = f"[{r['media_kind']}] {content}".strip()
        out.append(f"[{fmt_ts(r['date'])}] {r['from_name']}{flags}: {content}")
    data = "\n".join(out).encode()
    await msg.answer_document(BufferedInputFile(data, filename=f"chat_{args[0]}.txt"))


@router.message(Command("history"))
async def history(msg: Message, command: CommandObject):
    await export(msg, command, False)


@router.message(Command("deleted"))
async def deleted(msg: Message, command: CommandObject):
    await export(msg, command, True)


async def main():
    global db
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
    dp = Dispatcher()
    dp.include_router(router)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
