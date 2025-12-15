from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from config.config import SUPPORT_USERNAME, CHANNEL_USERNAME


def get_feedback_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👍", callback_data="like"),
            InlineKeyboardButton(text="👎", callback_data="dislike")
        ]
    ])

cancel_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="❌ Отмена")]
    ],
    resize_keyboard=True,
    one_time_keyboard=True
)


support_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Написать 💬", url=f"https://t.me/{SUPPORT_USERNAME}")]
    ]
)


channel_subscription_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Подписаться на канал", url=f"https://t.me/{CHANNEL_USERNAME.replace('@', '')}")],
        [InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_subscription")]
    ]
)