"""Клавиатуры (Reply и Inline)."""
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from config.config import CHANNEL_USERNAME, SUPPORT_USERNAME


def get_feedback_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👍", callback_data="like"),
            InlineKeyboardButton(text="👎", callback_data="dislike"),
        ]
    ])


cancel_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]],
    resize_keyboard=True,
    one_time_keyboard=True,
)


# В config username хранится с @ ("@boompi_ai_support") — для url его нужно убрать
support_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(
            text="Написать 💬",
            url=f"https://t.me/{SUPPORT_USERNAME.lstrip('@')}",
        )]
    ]
)


channel_subscription_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(
            text="📢 Подписаться на канал",
            url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}",
        )],
        [InlineKeyboardButton(
            text="✅ Проверить подписку",
            callback_data="check_subscription",
        )],
    ]
)
