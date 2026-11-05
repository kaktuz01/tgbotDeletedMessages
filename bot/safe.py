import asyncio
import logging

from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)

log = logging.getLogger("bot.safe")


async def safe(call, *args, retries: int = 4, **kwargs):
    """Call a Bot API method with retries. Returns None instead of raising."""
    for attempt in range(retries):
        try:
            return await call(*args, **kwargs)
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except (TelegramNetworkError, TelegramServerError):
            await asyncio.sleep(2 ** attempt)
        except (TelegramForbiddenError, TelegramBadRequest) as e:
            # blocked bot, "message is not modified", too-old callback, etc. - retrying won't help
            log.warning("%s: %s", getattr(call, "__name__", call), e)
            return None
    log.error("gave up after %s attempts: %s", retries, getattr(call, "__name__", call))
    return None
