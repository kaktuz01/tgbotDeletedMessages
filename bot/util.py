import html
import logging
import os
from datetime import datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo


def esc(s) -> str:
    return html.escape(str(s or ""))


@lru_cache
def get_tz():
    """Time zone from the TZ env var (e.g. Europe/Moscow); UTC if unset or unknown."""
    name = os.getenv("TZ") or "UTC"
    try:
        return ZoneInfo(name)
    except Exception:
        logging.getLogger("bot").warning("unknown TZ %r, falling back to UTC", name)
        return timezone.utc


def fmt_date(ts: int) -> str:
    return datetime.fromtimestamp(ts, get_tz()).strftime("%d.%m.%Y")


def fmt_short(ts: int) -> str:
    return datetime.fromtimestamp(ts, get_tz()).strftime("%d.%m %H:%M") if ts else "—"


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts, get_tz()).strftime("%d.%m.%Y %H:%M:%S")


def full_name(u) -> str:
    if not u:
        return "?"
    return (u.full_name or "") + (f" (@{u.username})" if u.username else "")


def quote(text, limit: int) -> str:
    """Escaped blockquote; truncated before escaping so HTML entities stay intact."""
    text = (text or "")[:limit]
    return f"<blockquote>{esc(text)}</blockquote>" if text else "<i>(пусто)</i>"
