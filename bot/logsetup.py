import logging
import os
import platform
import re
import subprocess
from datetime import datetime
from logging.handlers import RotatingFileHandler
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


FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_BYTES, LOG_BACKUPS = 2 * 1024 * 1024, 3  # at most ~8 MB on disk


def log_file() -> Path:
    """data/logs/bot.log next to the database (LOG_DIR overrides)."""
    d = os.getenv("LOG_DIR") or str(Path(os.getenv("DB_PATH", "data/bot.db")).parent / "logs")
    return Path(d) / "bot.log"


def setup_logging() -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        path = log_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(path, maxBytes=LOG_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8")
        )
    except OSError as e:  # read-only disk etc.: console logging still works
        print(f"cannot write log file: {e}")
    for h in handlers:
        h.setFormatter(TZFormatter(FORMAT, DATEFMT))
    root = logging.getLogger()
    root.handlers[:] = handlers
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


_STAMP = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} (\w+) ")


def read_tail(n: int) -> list[str]:
    """Last n lines of the log, reaching into rotated files when needed."""
    base = log_file()
    lines: list[str] = []
    for f in [base] + [base.with_name(f"{base.name}.{i}") for i in range(1, LOG_BACKUPS + 1)]:
        if not f.exists():
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines() + lines
        except OSError:
            continue
        if len(lines) >= n:
            break
    return lines[-n:]


def only_problems(lines: list[str]) -> list[str]:
    """WARNING/ERROR/CRITICAL records, including their traceback lines."""
    out, keep = [], False
    for line in lines:
        m = _STAMP.match(line)
        if m:
            keep = m.group(1) in ("WARNING", "ERROR", "CRITICAL")
        if keep:
            out.append(line)
    return out
