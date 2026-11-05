import html
from datetime import datetime


def esc(s) -> str:
    return html.escape(str(s or ""))


def fmt_ts(ts: int) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M:%S")


def full_name(u) -> str:
    if not u:
        return "?"
    return (u.full_name or "") + (f" (@{u.username})" if u.username else "")
