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
"""


class DB:
    def __init__(self, path: str):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def open(self):
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
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
        await self.conn.execute(
            "INSERT INTO messages (connection_id, chat_id, message_id, chat_name, from_id, from_name, "
            "date, text, media_kind, media_path) VALUES "
            "(:connection_id,:chat_id,:message_id,:chat_name,:from_id,:from_name,:date,:text,:media_kind,:media_path) "
            "ON CONFLICT(connection_id, chat_id, message_id) DO UPDATE SET "
            "text=excluded.text, media_kind=excluded.media_kind, "
            "media_path=COALESCE(excluded.media_path, media_path)",
            m,
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
            "GROUP BY m.chat_id ORDER BY MAX(m.date) DESC LIMIT 30",
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
