"""Главное меню (нижние команды бота)."""
from aiogram.types import BotCommand

from config.config import bot


async def set_main_menu() -> None:
    main_menu = [
        BotCommand(command="support", description="🛠 Техподдержка"),
    ]
    await bot.set_my_commands(main_menu)
