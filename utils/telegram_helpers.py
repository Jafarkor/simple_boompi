"""
Безопасные обёртки вокруг Telegram API.

Главное здесь — обработка двух классов ошибок:

1. TelegramRetryAfter (429 flood control) — ждём и ретраим.
2. TelegramBadRequest "message is not modified" — нормальная ситуация в конце
   стриминга, игнорируется.

Все edit-операции принимают reply_markup, потому что Telegram удаляет
inline-клавиатуру если её не передать в editMessageText. Кнопка [Отменить]
живёт под сообщением всё время стриминга и снимается только в финале.
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
)
from aiogram.methods import SendMessageDraft
from aiogram.types import InlineKeyboardMarkup, Message

from config.config import MAX_TELEGRAM_MESSAGE_LENGTH

logger = logging.getLogger(__name__)


_NOT_MODIFIED_FRAGMENT = "message is not modified"
_MAX_RETRIES = 4


async def safe_edit_text(
    message: Message,
    text: str,
    parse_mode: Optional[str] = "HTML",
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    raise_on_failure: bool = False,
) -> bool:
    """
    Редактирует сообщение с устойчивостью к флуд-лимитам и MessageNotModified.

    reply_markup нужно передавать на КАЖДОМ edit, иначе Telegram удалит кнопку.
    Если reply_markup=None — кнопка будет снята (используется на финальном edit).
    """
    for attempt in range(_MAX_RETRIES):
        try:
            await message.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
            return True

        except TelegramRetryAfter as e:
            wait = e.retry_after + random.uniform(0.1, 0.5)
            logger.warning(
                f"edit_text rate limited, sleeping {wait:.1f}s (attempt {attempt + 1})"
            )
            await asyncio.sleep(wait)
            continue

        except TelegramBadRequest as e:
            msg = str(e).lower()
            if _NOT_MODIFIED_FRAGMENT in msg:
                return True
            if parse_mode and attempt == 0:
                logger.warning(
                    f"edit_text BadRequest with parse_mode={parse_mode}, retrying as plain: {e}"
                )
                parse_mode = None
                continue
            logger.error(f"edit_text BadRequest unrecoverable: {e}")
            if raise_on_failure:
                raise
            return False

        except TelegramNetworkError as e:
            logger.warning(f"edit_text network error (attempt {attempt + 1}): {e}")
            await asyncio.sleep(0.5 * (attempt + 1))
            continue

        except Exception as e:
            logger.exception(f"edit_text unexpected error: {e}")
            if raise_on_failure:
                raise
            return False

    logger.error(f"edit_text failed after {_MAX_RETRIES} attempts")
    return False


async def safe_answer(
    message: Message,
    text: str,
    parse_mode: Optional[str] = "HTML",
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
    **kwargs,
) -> Optional[Message]:
    """Отправляет ответ с retry. None если не получилось."""
    for attempt in range(_MAX_RETRIES):
        try:
            return await message.answer(
                text, parse_mode=parse_mode, reply_markup=reply_markup, **kwargs
            )

        except TelegramRetryAfter as e:
            wait = e.retry_after + random.uniform(0.1, 0.5)
            logger.warning(f"answer rate limited, sleeping {wait:.1f}s")
            await asyncio.sleep(wait)
            continue

        except TelegramBadRequest as e:
            if parse_mode and attempt == 0:
                logger.warning(f"answer BadRequest with parse_mode, retrying as plain: {e}")
                parse_mode = None
                continue
            logger.error(f"answer BadRequest unrecoverable: {e}")
            return None

        except TelegramNetworkError as e:
            logger.warning(f"answer network error: {e}")
            await asyncio.sleep(0.5 * (attempt + 1))
            continue

        except Exception as e:
            logger.exception(f"answer unexpected error: {e}")
            return None

    return None


async def send_long_text(
    message: Message,
    text: str,
    parse_mode: Optional[str] = "HTML",
    *,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> Optional[Message]:
    """
    Отправляет ответ, разбивая на куски если он длиннее 4096 символов.
    reply_markup ставится только на ПЕРВУЮ часть.
    """
    if len(text) <= MAX_TELEGRAM_MESSAGE_LENGTH:
        return await safe_answer(message, text, parse_mode=parse_mode, reply_markup=reply_markup)

    chunks = _split_text(text, MAX_TELEGRAM_MESSAGE_LENGTH)
    first = None
    for i, chunk in enumerate(chunks):
        markup = reply_markup if i == 0 else None
        sent = await safe_answer(message, chunk, parse_mode=parse_mode, reply_markup=markup)
        if first is None:
            first = sent
    return first


def _split_text(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]

    parts: list[str] = []
    while len(text) > max_len:
        cut = text.rfind("\n\n", 0, max_len)
        if cut < max_len // 2:
            cut = text.rfind("\n", 0, max_len)
        if cut < max_len // 2:
            cut = text.rfind(" ", 0, max_len)
        if cut <= 0:
            cut = max_len
        parts.append(text[:cut])
        text = text[cut:].lstrip()
    if text:
        parts.append(text)
    return parts


# ────────────────────────────────────────────────────────────────────────────
# Native draft streaming (Bot API 9.5+)
# ────────────────────────────────────────────────────────────────────────────
async def send_message_draft(
    bot: Bot,
    chat_id: int,
    draft_id: int,
    text: str,
    parse_mode: Optional[str] = "HTML",
) -> bool:
    """Стримит частичное сообщение через sendMessageDraft. False — fallback нужен."""
    try:
        await bot(SendMessageDraft(
            chat_id=chat_id,
            draft_id=draft_id,
            text=text,
            parse_mode=parse_mode,
        ))
        return True
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after + 0.2)
        try:
            await bot(SendMessageDraft(
                chat_id=chat_id,
                draft_id=draft_id,
                text=text,
                parse_mode=parse_mode,
            ))
            return True
        except Exception:
            return False
    except TelegramBadRequest:
        return False
    except Exception as e:
        logger.warning(f"sendMessageDraft failed, falling back to edit_text: {e}")
        return False
