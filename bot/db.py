import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    user_chat_id INTEGER NOT NULL,
    is_enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS messages (
    connection_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    chat_name TEXT,
    from_id INTEGER,
    from_name TEXT,
    date INTEGER NOT NULL,
    text TEXT,
    media_kind TEXT,
    media_path TEXT,
    media_file_id TEXT,
    edited INTEGER NOT NULL DEFAULT 0,
    deleted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (connection_id, chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_msg_chat ON messages (connection_id, chat_id, date);
CREATE TABLE IF NOT EXISTS edits (
    connection_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    old_text TEXT,
    edited_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    user_id INTEGER PRIMARY KEY,
    notify_deleted INTEGER NOT NULL DEFAULT 1,
    notify_edited INTEGER NOT NULL DEFAULT 1,
    notify_own INTEGER NOT NULL DEFAULT 0,
    backup_daily INTEGER NOT NULL DEFAULT 0,
    last_backup INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS ignored (
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    chat_name TEXT,
    PRIMARY KEY (user_id, chat_id)
);
"""

SCHEMA += """
CREATE TABLE IF NOT EXISTS access (
    user_id INTEGER PRIMARY KEY,
    trial_until INTEGER NOT NULL DEFAULT 0,
    paid_until INTEGER NOT NULL DEFAULT 0,
    last_nudge INTEGER NOT NULL DEFAULT 0,
    free INTEGER NOT NULL DEFAULT 0,
    name TEXT
);
CREATE TABLE IF NOT EXISTS payments (
    charge_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    stars INTEGER NOT NULL,
    ts INTEGER NOT NULL
);
"""

SCHEMA += """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    name TEXT,
    last_seen INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_users_username ON users (username);
CREATE TABLE IF NOT EXISTS pending_free (username TEXT PRIMARY KEY);
"""

SETTING_FIELDS = ("notify_deleted", "notify_edited", "notify_own", "backup_daily")


class DB:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def open(self):
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        # migration for databases created before media_file_id existed
        async with self.conn.execute("PRAGMA table_info(messages)") as c:
            cols = {r["name"] for r in await c.fetchall()}
        if "media_file_id" not in cols:
            await self.conn.execute("ALTER TABLE messages ADD COLUMN media_file_id TEXT")
        await self.conn.commit()

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def upsert_connection(self, cid, user_id, user_chat_id, enabled):
        await self.conn.execute(
            "INSERT INTO connections VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
            "user_id=excluded.user_id, user_chat_id=excluded.user_chat_id, is_enabled=excluded.is_enabled",
            (cid, user_id, user_chat_id, int(enabled)),
        )
        await self.conn.commit()

    async def get_connection(self, cid):
        async with self.conn.execute("SELECT * FROM connections WHERE id=?", (cid,)) as c:
            return await c.fetchone()

    async def connections_of_user(self, user_id):
        async with self.conn.execute("SELECT * FROM connections WHERE user_id=?", (user_id,)) as c:
            return await c.fetchall()

    async def save_message(self, **m):
        m.setdefault("media_file_id", None)
        await self.conn.execute(
            "INSERT INTO messages (connection_id, chat_id, message_id, chat_name, from_id, from_name, "
            "date, text, media_kind, media_path, media_file_id) VALUES "
            "(:connection_id,:chat_id,:message_id,:chat_name,:from_id,:from_name,:date,:text,"
            ":media_kind,:media_path,:media_file_id) "
            "ON CONFLICT(connection_id, chat_id, message_id) DO UPDATE SET "
            "text=excluded.text, media_kind=excluded.media_kind, "
            # keep the downloaded file unless the message now carries a different file
            "media_path=CASE WHEN excluded.media_file_id IS media_file_id "
            "THEN media_path ELSE excluded.media_path END, "
            "media_file_id=COALESCE(excluded.media_file_id, media_file_id)",
            m,
        )
        await self.conn.commit()

    async def set_media_path(self, cid, chat_id, message_id, path):
        await self.conn.execute(
            "UPDATE messages SET media_path=? WHERE connection_id=? AND chat_id=? AND message_id=?",
            (path, cid, chat_id, message_id),
        )
        await self.conn.commit()

    async def get_message(self, cid, chat_id, message_id):
        async with self.conn.execute(
            "SELECT * FROM messages WHERE connection_id=? AND chat_id=? AND message_id=?",
            (cid, chat_id, message_id),
        ) as c:
            return await c.fetchone()

    async def mark_edited(self, cid, chat_id, message_id, old_text, ts):
        await self.conn.execute(
            "INSERT INTO edits VALUES (?,?,?,?,?)", (cid, chat_id, message_id, old_text, ts)
        )
        await self.conn.execute(
            "UPDATE messages SET edited=1 WHERE connection_id=? AND chat_id=? AND message_id=?",
            (cid, chat_id, message_id),
        )
        await self.conn.commit()

    async def mark_deleted(self, cid, chat_id, message_ids):
        await self.conn.executemany(
            "UPDATE messages SET deleted=1 WHERE connection_id=? AND chat_id=? AND message_id=?",
            [(cid, chat_id, i) for i in message_ids],
        )
        await self.conn.commit()

    async def chats(self, user_id):
        async with self.conn.execute(
            "SELECT m.chat_id, m.chat_name, COUNT(*) n, SUM(m.deleted) d FROM messages m "
            "JOIN connections c ON c.id=m.connection_id WHERE c.user_id=? "
            "GROUP BY m.chat_id ORDER BY MAX(m.date) DESC LIMIT 200",
            (user_id,),
        ) as c:
            return await c.fetchall()

    async def history(self, user_id, chat_id, limit=None, deleted_only=False):
        q = (
            "SELECT m.* FROM messages m JOIN connections c ON c.id=m.connection_id "
            "WHERE c.user_id=? AND m.chat_id=? "
        )
        if deleted_only:
            q += "AND m.deleted=1 "
        q += "ORDER BY m.date DESC"
        args = [user_id, chat_id]
        if limit:
            q += " LIMIT ?"
            args.append(limit)
        async with self.conn.execute(q, args) as c:
            rows = await c.fetchall()
        return list(reversed(rows))

    # ---- settings / ignore / backup ----

    async def get_settings(self, user_id) -> dict:
        await self.conn.execute("INSERT OR IGNORE INTO settings (user_id) VALUES (?)", (user_id,))
        await self.conn.commit()
        async with self.conn.execute("SELECT * FROM settings WHERE user_id=?", (user_id,)) as c:
            return dict(await c.fetchone())

    async def toggle_setting(self, user_id, field):
        if field not in SETTING_FIELDS:
            raise ValueError(field)
        await self.get_settings(user_id)
        await self.conn.execute(f"UPDATE settings SET {field} = 1 - {field} WHERE user_id=?", (user_id,))
        await self.conn.commit()

    async def set_last_backup(self, user_id, ts):
        await self.conn.execute("UPDATE settings SET last_backup=? WHERE user_id=?", (ts, user_id))
        await self.conn.commit()

    async def due_backups(self, now, every=86400):
        async with self.conn.execute(
            "SELECT user_id FROM settings WHERE backup_daily=1 AND last_backup <= ?", (now - every,)
        ) as c:
            return [r["user_id"] for r in await c.fetchall()]

    async def is_ignored(self, user_id, chat_id) -> bool:
        async with self.conn.execute(
            "SELECT 1 FROM ignored WHERE user_id=? AND chat_id=?", (user_id, chat_id)
        ) as c:
            return await c.fetchone() is not None

    async def set_ignored(self, user_id, chat_id, name, flag: bool):
        if flag:
            await self.conn.execute(
                "INSERT OR REPLACE INTO ignored VALUES (?,?,?)", (user_id, chat_id, name)
            )
        else:
            await self.conn.execute(
                "DELETE FROM ignored WHERE user_id=? AND chat_id=?", (user_id, chat_id)
            )
        await self.conn.commit()

    async def ignored_list(self, user_id):
        async with self.conn.execute(
            "SELECT chat_id, chat_name FROM ignored WHERE user_id=? ORDER BY chat_name", (user_id,)
        ) as c:
            return await c.fetchall()

    async def chat_name(self, user_id, chat_id) -> str:
        async with self.conn.execute(
            "SELECT m.chat_name FROM messages m JOIN connections c ON c.id=m.connection_id "
            "WHERE c.user_id=? AND m.chat_id=? LIMIT 1",
            (user_id, chat_id),
        ) as c:
            r = await c.fetchone()
        return (r["chat_name"] if r else None) or str(chat_id)

    async def chat_stats(self, user_id, chat_id):
        async with self.conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(m.deleted),0) d, COALESCE(SUM(m.edited),0) e "
            "FROM messages m JOIN connections c ON c.id=m.connection_id "
            "WHERE c.user_id=? AND m.chat_id=?",
            (user_id, chat_id),
        ) as c:
            return await c.fetchone()

    # ---- access / payments ----

    async def get_access(self, user_id):
        async with self.conn.execute("SELECT * FROM access WHERE user_id=?", (user_id,)) as c:
            r = await c.fetchone()
        return dict(r) if r else None

    async def start_trial(self, user_id, days, now):
        await self.conn.execute("INSERT OR IGNORE INTO access (user_id) VALUES (?)", (user_id,))
        await self.conn.execute(
            "UPDATE access SET trial_until=? WHERE user_id=? AND trial_until=0 AND paid_until=0",
            (now + days * 86400, user_id),
        )
        await self.conn.commit()

    async def add_paid(self, user_id, days, charge_id, stars, now) -> int | None:
        """Extend the subscription. Returns new paid_until, or None for a duplicate charge."""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO payments VALUES (?,?,?,?)", (charge_id, user_id, stars, now)
        )
        if cur.rowcount == 0:
            await self.conn.commit()
            return None
        await self.conn.execute("INSERT OR IGNORE INTO access (user_id) VALUES (?)", (user_id,))
        await self.conn.execute(
            "UPDATE access SET paid_until = MAX(?, paid_until) + ? WHERE user_id=?",
            (now, days * 86400, user_id),
        )
        await self.conn.commit()
        return (await self.get_access(user_id))["paid_until"]

    async def touch_nudge(self, user_id, ts):
        await self.conn.execute("UPDATE access SET last_nudge=? WHERE user_id=?", (ts, user_id))
        await self.conn.commit()

    async def set_free(self, user_id, flag: bool, name=None):
        await self.conn.execute("INSERT OR IGNORE INTO access (user_id) VALUES (?)", (user_id,))
        await self.conn.execute(
            "UPDATE access SET free=?, name=COALESCE(?, name) WHERE user_id=?", (int(flag), name, user_id)
        )
        await self.conn.commit()

    async def free_list(self):
        async with self.conn.execute(
            "SELECT a.user_id, COALESCE(u.username, '') username, COALESCE(a.name, u.name) name "
            "FROM access a LEFT JOIN users u ON u.user_id = a.user_id WHERE a.free=1 ORDER BY name"
        ) as c:
            return await c.fetchall()

    async def stats(self, now):
        async with self.conn.execute(
            "SELECT COUNT(*) users, COALESCE(SUM(free=1),0) friends, "
            "COALESCE(SUM(paid_until>?),0) paid, "
            "COALESCE(SUM(trial_until>? AND paid_until<=? AND free=0),0) trial FROM access",
            (now, now, now),
        ) as c:
            r = dict(await c.fetchone())
        async with self.conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(stars),0) stars FROM payments"
        ) as c:
            p = await c.fetchone()
        r["payments"], r["stars"] = p["n"], p["stars"]
        return r

    # ---- users directory (for granting access by @username) ----

    async def touch_user(self, user_id, username, name, now) -> bool:
        """Remember a user who talks to the bot. True if a pending free grant was just applied."""
        uname = username.lower() if username else None
        await self.conn.execute(
            "INSERT INTO users (user_id, username, name, last_seen) VALUES (?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, name=excluded.name, "
            "last_seen=excluded.last_seen",
            (user_id, uname, name, now),
        )
        granted = False
        if uname:
            cur = await self.conn.execute("DELETE FROM pending_free WHERE username=?", (uname,))
            if cur.rowcount:
                await self.conn.execute("INSERT OR IGNORE INTO access (user_id) VALUES (?)", (user_id,))
                await self.conn.execute(
                    "UPDATE access SET free=1, name=? WHERE user_id=?", (name, user_id)
                )
                granted = True
        await self.conn.commit()
        return granted

    async def user_id_by_username(self, username):
        async with self.conn.execute(
            "SELECT user_id FROM users WHERE username=? ORDER BY last_seen DESC LIMIT 1",
            (username.lower(),),
        ) as c:
            r = await c.fetchone()
        return r["user_id"] if r else None

    async def add_pending(self, username):
        await self.conn.execute("INSERT OR IGNORE INTO pending_free VALUES (?)", (username.lower(),))
        await self.conn.commit()

    async def del_pending(self, username) -> bool:
        cur = await self.conn.execute("DELETE FROM pending_free WHERE username=?", (username.lower(),))
        await self.conn.commit()
        return cur.rowcount > 0

    async def pending_list(self):
        async with self.conn.execute("SELECT username FROM pending_free ORDER BY username") as c:
            return [r["username"] for r in await c.fetchall()]
