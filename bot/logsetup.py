import logging
import platform
import subprocess
from datetime import datetime
from pathlib import Path

import aiogram

from .billing import ADMINS, PLAN_DAYS, PRICE_STARS, TRIAL_DAYS
from .db import DB
from .safe import safe
from .util import get_tz
from .version import __version__

log = logging.getLogger("bot")


class TZFormatter(logging.Formatter):
    """Timestamps in the time zone from TZ (default UTC)."""

    converter = staticmethod(lambda ts: datetime.fromtimestamp(ts, get_tz()).timetuple())


def setup_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        TZFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    # one line per handled update drowns everything else
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


def _git_hash() -> str | None:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).parent, capture_output=True, text=True, timeout=3,
        )
        return r.stdout.strip() or None if r.returncode == 0 else None
    except Exception:
        return None


async def log_startup(bot, db: DB, media_dir) -> None:
    """Startup banner: which version runs, with which settings. Never prints secrets."""
    git = _git_hash()
    me = await safe(bot.get_me)
    log.info(
        "=== start | version %s%s | python %s | aiogram %s | tz %s ===",
        __version__, f" ({git})" if git else "", platform.python_version(), aiogram.__version__, get_tz(),
    )
    if me:
        log.info("bot: @%s id=%s", me.username, me.id)
    log.info(
        "config: price_stars=%s plan_days=%s trial_days=%s admins=%s db=%s media_dir=%s",
        PRICE_STARS, PLAN_DAYS, TRIAL_DAYS, len(ADMINS), db.path, media_dir,
    )
    c = await db.counts()
    log.info(
        "db: connections=%s messages=%s users=%s payments=%s",
        c["connections"], c["messages"], c["users"], c["payments"],
    )
