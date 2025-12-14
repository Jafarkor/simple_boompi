from config.config import bot
from aiogram.types import BotCommand



async def set_main_menu():
    main_menu = [
        BotCommand(command="help", description="❔  Вопросы (FAQ)"),
        BotCommand(command="support", description="🛠️  Техподдержка"),
    ]
    await bot.set_my_commands(main_menu)