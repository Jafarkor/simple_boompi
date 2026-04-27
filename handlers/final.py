"""Fallback handler — ловит всё что не подошло предыдущим роутерам."""
import logging

from aiogram import Router
from aiogram.types import CallbackQuery, Message

from lexicon.lexicon import LEXICON_RU as lexicon
from utils.telegram_helpers import safe_answer

logger = logging.getLogger(__name__)
rt = Router()


@rt.callback_query()
async def unknown_callback(callback: CallbackQuery) -> None:
    """Неизвестный callback — просто отмечаем что обработали, без шума пользователю."""
    logger.info(f"Unknown callback data={callback.data!r} from {callback.from_user.id}")
    await callback.answer()


@rt.message()
async def other_messages(msg: Message) -> None:
    """Стикеры/анимации/локации/контакты и прочее — не поддерживаем."""
    logger.info(
        f"Unsupported message type from {msg.from_user.id}: "
        f"content_type={msg.content_type}"
    )
    await safe_answer(
        msg,
        "Я пока не умею обрабатывать такой тип сообщений. "
        "Отправь мне текст, голосовое, фото или документ (.pdf/.docx/.txt).",
    )
