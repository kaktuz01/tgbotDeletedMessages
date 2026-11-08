import asyncio
import gzip
import logging
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime
from pathlib import Path

from aiogram import Bot
from aiogram.types import FSInputFile

from .billing import status
from .db import DB
from .safe import safe
from .util import get_tz

log = logging.getLogger("bot.backup")
TG_LIMIT = 49 * 1024 * 1024


def _make_user_backup(db_path: str, user_id: int, out_dir: str) -> str:
    """Copy the DB and strip everything that does not belong to this user."""
    tmp = Path(out_dir) / "copy.db"
    src = sqlite3.connect(db_path)
    try:
        src.execute("VACUUM INTO ?", (str(tmp),))
    finally:
        src.close()
    c = sqlite3.connect(tmp)
    try:
        mine = "(SELECT id FROM connections WHERE user_id = ?)"
        c.execute(f"DELETE FROM messages WHERE connection_id NOT IN {mine}", (user_id,))
        c.execute(f"DELETE FROM edits WHERE connection_id NOT IN {mine}", (user_id,))
        c.execute("DELETE FROM connections WHERE user_id != ?", (user_id,))
        c.execute("DELETE FROM settings WHERE user_id != ?", (user_id,))
        c.execute("DELETE FROM ignored WHERE user_id != ?", (user_id,))
        c.commit()
        c.execute("VACUUM")
    finally:
        c.close()
    out = Path(out_dir) / f"backup_{datetime.now(get_tz()):%Y%m%d_%H%M}.db.gz"
    with open(tmp, "rb") as f_in, gzip.open(out, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    tmp.unlink()
    return str(out)


async def send_backup(bot: Bot, db: DB, user_id: int) -> bool:
    conns = await db.connections_of_user(user_id)
    chat_id = conns[0]["user_chat_id"] if conns else user_id
    with tempfile.TemporaryDirectory() as d:
        try:
            path = await asyncio.to_thread(_make_user_backup, db.path, user_id, d)
        except Exception:
            log.exception("backup failed for %s", user_id)
            return False
        if Path(path).stat().st_size > TG_LIMIT:
            await safe(bot.send_message, chat_id, "⚠️ Бэкап больше 49 МБ, Telegram его не примет.")
            return False
        r = await safe(
            bot.send_document,
            chat_id,
            FSInputFile(path),
            caption="📦 Бэкап ваших данных (SQLite, gzip). Медиафайлы в него не входят.",
        )
    if r is not None:
        await db.set_last_backup(user_id, int(time.time()))
        return True
    return False


async def backup_loop(bot: Bot, db: DB):
    await asyncio.sleep(60)
    while True:
        try:
            for uid in await db.due_backups(int(time.time())):
                if (await status(db, uid))[0]:
                    await send_backup(bot, db, uid)
        except Exception:
            log.exception("backup loop error")
        await asyncio.sleep(3600)
