"""Глобальный middleware — логирование и ловля непойманных исключений."""
import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update, User

logger = logging.getLogger(__name__)


class GeneralMiddleware(BaseMiddleware):
    """
    Логирует входящие апдейты с временем обработки.
    Ловит ВСЕ исключения, чтобы не было «Task exception was never retrieved».
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        user_id = user.id if user else "unknown"

        start = time.monotonic()
        try:
            result = await handler(event, data)
            elapsed = time.monotonic() - start
            if elapsed > 5.0:  # медленные хендлеры — в лог
                logger.info(f"Handler took {elapsed:.1f}s for user {user_id}")
            return result

        except Exception as e:
            elapsed = time.monotonic() - start
            logger.exception(
                f"Unhandled exception in handler for user {user_id} "
                f"after {elapsed:.1f}s: {e}"
            )
            return None
