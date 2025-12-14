from aiogram import Router
from aiogram.filters import CommandStart, Command
from keyboards.keyboards import support_keyboard
from aiogram.fsm.context import FSMContext
from aiogram.types import Message, FSInputFile
from lexicon.lexicon import LEXICON_RU as lexicon
import logging

logger = logging.getLogger(__name__)
rt = Router()

@rt.message(CommandStart())
async def start(msg: Message, state: FSMContext):
    await msg.answer(lexicon['greeting'])
    await state.clear()

@rt.message(Command("help"))
async def show_help(msg: Message):
    await msg.answer(lexicon['help'])

@rt.message(Command("support"))
async def support(msg: Message):
    photo = FSInputFile("media/support.jpg")
    await msg.answer_photo(photo=photo, caption=lexicon['support'], reply_markup=support_keyboard)