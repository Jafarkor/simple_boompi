from aiogram import Router
from aiogram.types import Message, CallbackQuery
from lexicon.lexicon import LEXICON_RU as lexicon


rt = Router()


@rt.callback_query()
async def not_working(callback: CallbackQuery):
    print('YES')
    await callback.message.answer(lexicon['error'])
    await callback.answer()

@rt.message()
async def other_messages(msg: Message):
    await msg.answer(lexicon['error'])