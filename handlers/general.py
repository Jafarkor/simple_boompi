"""Простые команды: /start, /support."""
import logging

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile, Message

from keyboards.keyboards import support_keyboard
from lexicon.lexicon import LEXICON_RU as lexicon
from utils.telegram_helpers import safe_answer

logger = logging.getLogger(__name__)
rt = Router()


@rt.message(CommandStart())
async def start(msg: Message, state: FSMContext) -> None:
    await state.clear()
    await safe_answer(msg, lexicon["greeting"])


@rt.message(Command("support"))
async def support(msg: Message) -> None:
    photo = FSInputFile("media/support.jpg")
    try:
        await msg.answer_photo(
            photo=photo,
            caption=lexicon["support"],
            reply_markup=support_keyboard,
        )
    except Exception as e:
        logger.error(f"Failed to send support photo: {e}")
        await safe_answer(msg, lexicon["support"], reply_markup=support_keyboard)
