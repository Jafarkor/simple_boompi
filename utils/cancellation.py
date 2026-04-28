"""
Реестр in-flight asyncio-задач + клавиатура для их отмены пользователем.

Когда пользователь шлёт запрос, мы:
1. Отправляем loader-сообщение с кнопкой [Отменить] и callback_data
   `cancel:{chat_id}:{message_id}`.
2. Заворачиваем работу в asyncio.Task и регистрируем здесь по ключу
   (chat_id, message_id).
3. Если пользователь жмёт кнопку — callback-хендлер вызывает cancel_task(),
   который делает task.cancel(). CancelledError пробрасывается через стрим
   и httpx-сессии, реально обрывая запрос к OpenAI/Groq.
4. Хендлер задачи ловит CancelledError и редактирует loader в «Отменено».
5. add_done_callback автоматом удаляет запись из реестра.

Реестр in-memory, не Redis — задачи живут только в этом процессе, нет смысла
синхронизировать. Если процесс упадёт, все запросы умрут вместе с ним.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional, Tuple

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

# (chat_id, message_id) → asyncio.Task
_active_tasks: dict[Tuple[int, int], asyncio.Task] = {}

CANCEL_CB_PREFIX = "cancel:"


def make_cancel_keyboard(chat_id: int, message_id: int) -> InlineKeyboardMarkup:
    """Inline-кнопка под сообщением бота для отмены текущего запроса."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="Отменить",
                callback_data=f"{CANCEL_CB_PREFIX}{chat_id}:{message_id}",
            )
        ]]
    )


def register_task(chat_id: int, message_id: int, task: asyncio.Task) -> None:
    """Регистрирует задачу. По её завершении автоматически удалится из реестра."""
    key = (chat_id, message_id)
    _active_tasks[key] = task

    def _cleanup(_t: asyncio.Task) -> None:
        _active_tasks.pop(key, None)
        logger.debug(
            f"Task removed from registry: ({chat_id}, {message_id}); "
            f"active total: {len(_active_tasks)}"
        )

    task.add_done_callback(_cleanup)
    logger.debug(
        f"Task registered: ({chat_id}, {message_id}); active total: {len(_active_tasks)}"
    )


def cancel_task(chat_id: int, message_id: int) -> bool:
    """
    Отменяет задачу. Возвращает True если задача была активна и реально
    cancel(), False если задачи уже нет (завершилась или не существовала).
    """
    key = (chat_id, message_id)
    task = _active_tasks.get(key)
    if task is None:
        logger.debug(f"cancel_task: no active task for ({chat_id}, {message_id})")
        return False
    if task.done():
        logger.debug(f"cancel_task: task already done for ({chat_id}, {message_id})")
        return False
    task.cancel()
    logger.info(f"Task cancelled by user for ({chat_id}, {message_id})")
    return True


def parse_cancel_data(data: str) -> Optional[Tuple[int, int]]:
    """Парсит callback_data вида 'cancel:{chat_id}:{message_id}'."""
    if not data or not data.startswith(CANCEL_CB_PREFIX):
        return None
    try:
        rest = data[len(CANCEL_CB_PREFIX):]
        chat_str, msg_str = rest.split(":", 1)
        return int(chat_str), int(msg_str)
    except (ValueError, IndexError):
        logger.warning(f"Failed to parse cancel callback data: {data!r}")
        return None
