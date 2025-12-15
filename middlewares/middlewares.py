from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from typing import Callable, Any
import logging
from config.config import redis

redis_client = redis.from_url("redis://localhost")


logger = logging.getLogger(__name__)


import logging

class GeneralMiddleware(BaseMiddleware):
    async def __call__(self,
                       handler: Callable,
                       event: TelegramObject,
                       data: dict[str, Any]) -> Any:
        user: User | None = data["event_from_user"]

        try:
            return await handler(event, data)

        except Exception as e:
            print(e)

        return