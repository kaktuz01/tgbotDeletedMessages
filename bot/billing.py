import os
import time
from datetime import datetime

from .db import DB

DAY = 86400
PRICE_STARS = int(os.getenv("PRICE_STARS", "50"))
TRIAL_DAYS = int(os.getenv("TRIAL_DAYS", "3"))
PLAN_DAYS = int(os.getenv("PLAN_DAYS", "30"))
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}


def classify(user_id: int, free, paid_until: int, trial_until: int, now: int):
    """Pure access rule. Returns (active, kind, until); kind: admin|free|paid|trial|expired|none."""
    if user_id in ADMINS:
        return True, "admin", 0
    if free:
        return True, "free", 0
    if paid_until > now:
        return True, "paid", paid_until
    if trial_until > now:
        return True, "trial", trial_until
    last = max(paid_until, trial_until)
    return (False, "expired", last) if last else (False, "none", 0)


async def status(db: DB, user_id: int, now: int | None = None):
    """Return (active, kind, until). kind: admin | paid | trial | expired | none."""
    now = now or int(time.time())
    if user_id in ADMINS:
        return True, "admin", 0
    a = await db.get_access(user_id)
    if a is None:
        if await db.connections_of_user(user_id):  # connected before billing existed
            await db.start_trial(user_id, TRIAL_DAYS, now)
            a = await db.get_access(user_id)
        else:
            return False, "none", 0
    return classify(user_id, a["free"], a["paid_until"], a["trial_until"], now)


def describe(kind: str, until: int) -> str:
    d = datetime.fromtimestamp(until).strftime("%d.%m.%Y") if until else ""
    return {
        "admin": "👑 Безлимитный доступ",
        "free": "🤝 Бесплатный доступ от автора",
        "paid": f"💎 Подписка активна до <b>{d}</b>",
        "trial": f"🎁 Пробный период до <b>{d}</b>",
        "expired": f"⏳ Доступ закончился <b>{d}</b>",
        "none": f"🎁 Пробный период {TRIAL_DAYS} дн. начнётся после подключения бота",
    }[kind]
