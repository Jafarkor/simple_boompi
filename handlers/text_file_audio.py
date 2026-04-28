"""Обработчик text/voice/document/photo сообщений.

Главные новые фичи:
- Кнопка [Отменить] под loader-ом и под стрим-сообщением — реально
  отменяет async-задачу (через task.cancel()).
- Детальный DEBUG-лог: для каждого запроса видно тайминг, стадию (analyze /
  stream / final), статус (OK / FAIL / CANCELLED), размеры данных и счётчики.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from contextlib import asynccontextmanager, suppress
from pathlib import Path

import aiofiles
from aiogram import F, Router
from aiogram.enums.chat_action import ChatAction
from aiogram.enums.chat_member_status import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    Message,
    ReactionTypeEmoji,
)
from aiogram.utils.chat_action import ChatActionSender

from config.config import (
    bot,
    redis,
    groq_client,
    CHANNEL_USERNAME,
    MAX_WORD_COUNT,
    MAX_IMAGES_PER_REQUEST,
    USE_STREAM,
    USE_NATIVE_DRAFT_STREAM,
    TIME_STREAM_UPDATE,
    STREAM_MIN_CHUNK_SIZE,
    STREAM_MAX_CHUNK_SIZE,
    USER_LOCK_TTL,
)
from keyboards.keyboards import channel_subscription_keyboard
from lexicon.lexicon import LEXICON_RU as lexicon
from utils.cancellation import (
    CANCEL_CB_PREFIX,
    cancel_task,
    make_cancel_keyboard,
    parse_cancel_data,
    register_task,
)
from utils.functions import (
    generate_code,
    markdown_to_telegram_html,
    process_audio_with_whisper,
    process_request,
    read_docx,
    read_pdf,
    read_txt,
    save_context,
)
from utils.logging_helpers import log_event, log_timing
from utils.telegram_helpers import (
    safe_answer,
    safe_edit_text,
    send_long_text,
    send_message_draft,
)
from utils.universal_analyzer import UniversalAnalyzer

logger = logging.getLogger(__name__)
rt = Router()

POPULAR_EMOJIS = ["👍", "❤️", "🔥", "😍", "🎉", "😢", "🤔", "😡", "😭", "😴", "🤯"]

DOCUMENTS_DIR = Path("documents")
DOCUMENTS_DIR.mkdir(exist_ok=True)

analyzer = UniversalAnalyzer(groq_client)


# ────────────────────────────────────────────────────────────────────────────
# Per-user lock
# ────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def user_lock(user_id: int):
    """SET NX EX в Redis — параллельные сообщения от одного юзера сериализуются."""
    key = f"user:{user_id}:lock"
    acquired = await redis.set(key, b"1", ex=USER_LOCK_TTL, nx=True)
    if not acquired:
        log_event("user_lock.busy", user=user_id)
        yield False
        return
    log_event("user_lock.acquired", user=user_id)
    try:
        yield True
    finally:
        with suppress(Exception):
            await redis.delete(key)
        log_event("user_lock.released", user=user_id)


# ────────────────────────────────────────────────────────────────────────────
# Подписка на канал
# ────────────────────────────────────────────────────────────────────────────
async def is_subscribed(user_id: int) -> bool:
    try:
        async with log_timing("telegram.get_chat_member", user=user_id, channel=CHANNEL_USERNAME):
            member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
        )
    except Exception as e:
        logger.error(f"Subscription check failed for {user_id}: {e}")
        return False


async def check_subscription(msg: Message) -> bool:
    if await is_subscribed(msg.from_user.id):
        return True
    await safe_answer(
        msg,
        "💙 Чтобы пользоваться ботом необходимо подписаться на наш официальный канал",
        reply_markup=channel_subscription_keyboard,
    )
    return False


# ────────────────────────────────────────────────────────────────────────────
# Streaming
# ────────────────────────────────────────────────────────────────────────────
async def _stream_via_edit_text(
    msg: Message,
    stream_response,
    initial_message: Message | None = None,
    cancel_markup: InlineKeyboardMarkup | None = None,
) -> str:
    """
    Стриминг через editMessageText с кнопкой [Отменить].

    cancel_markup передаётся на КАЖДОМ edit, иначе Telegram удаляет кнопку.
    На финальном edit передаётся reply_markup=None — кнопка снимается.
    """
    full_response = ""
    buffer = ""
    sent_message: Message | None = None
    last_shown_html = ""
    last_update = time.monotonic() - TIME_STREAM_UPDATE
    stream_error: Exception | None = None
    chunks_received = 0
    edits_done = 0

    if initial_message is not None:
        sent_message = initial_message

    stream_start = time.monotonic()

    try:
        async for chunk in stream_response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if not delta:
                continue

            chunks_received += 1
            buffer += delta
            full_response += delta

            elapsed = time.monotonic() - last_update
            ready_to_show = (
                len(buffer) >= STREAM_MAX_CHUNK_SIZE
                or (elapsed >= TIME_STREAM_UPDATE and len(buffer) >= STREAM_MIN_CHUNK_SIZE)
            )
            if not ready_to_show:
                continue

            html_now = markdown_to_telegram_html(full_response)
            if not html_now.strip() or html_now == last_shown_html:
                buffer = ""
                continue

            if sent_message is None:
                sent_message = await safe_answer(
                    msg, html_now, parse_mode="HTML", reply_markup=cancel_markup
                )
                if sent_message is not None:
                    last_shown_html = html_now
                    last_update = time.monotonic()
                    edits_done += 1
            else:
                ok = await safe_edit_text(
                    sent_message, html_now, parse_mode="HTML", reply_markup=cancel_markup
                )
                if ok:
                    last_shown_html = html_now
                    last_update = time.monotonic()
                    edits_done += 1

            buffer = ""

    except asyncio.CancelledError:
        # Не глотаем — это сигнал отмены пользователем или shutdown'а
        log_event(
            "stream.cancelled",
            chunks=chunks_received,
            edits=edits_done,
            chars=len(full_response),
            took_ms=int((time.monotonic() - stream_start) * 1000),
        )
        raise
    except Exception as e:
        logger.warning(f"Stream interrupted mid-flight: {e}")
        stream_error = e

    # ── Финал: всегда выводим то, что успели накопить (без cancel-кнопки) ──
    if not full_response.strip():
        if stream_error:
            raise stream_error
        return full_response

    final_html = markdown_to_telegram_html(full_response)

    if sent_message is None:
        await send_long_text(msg, final_html, parse_mode="HTML")
    elif final_html != last_shown_html:
        # На финале reply_markup=None — кнопка [Отменить] исчезает
        ok = await safe_edit_text(sent_message, final_html, parse_mode="HTML", reply_markup=None)
        if not ok:
            logger.warning("Final edit failed — sending as a new message to ensure delivery")
            await send_long_text(msg, final_html, parse_mode="HTML")
    else:
        # Текст не изменился, но кнопку убрать всё равно надо
        with suppress(Exception):
            await sent_message.edit_reply_markup(reply_markup=None)

    log_event(
        "stream.done",
        chunks=chunks_received,
        edits=edits_done,
        chars=len(full_response),
        took_ms=int((time.monotonic() - stream_start) * 1000),
        error=type(stream_error).__name__ if stream_error else None,
    )
    return full_response


async def _stream_via_native_draft(
    msg: Message,
    stream_response,
    initial_message: Message | None = None,
    cancel_markup: InlineKeyboardMarkup | None = None,
) -> str:
    """Native draft streaming через sendMessageDraft (Bot API 9.5+)."""
    full_response = ""
    buffer = ""
    last_update = time.monotonic() - TIME_STREAM_UPDATE
    draft_id = random.randint(1, 2**31 - 1)
    chat_id = msg.chat.id
    drafts_supported = True
    stream_error: Exception | None = None

    # В native режиме loader не нужен (drafts отображаются в отдельном bubble)
    if initial_message is not None:
        with suppress(Exception):
            await initial_message.delete()

    try:
        async for chunk in stream_response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if not delta:
                continue

            buffer += delta
            full_response += delta

            elapsed = time.monotonic() - last_update
            ready = (
                len(buffer) >= STREAM_MAX_CHUNK_SIZE
                or (elapsed >= 0.5 and len(buffer) >= STREAM_MIN_CHUNK_SIZE)
            )
            if not ready or not drafts_supported:
                continue

            html_now = markdown_to_telegram_html(full_response)
            if not html_now.strip():
                buffer = ""
                continue

            ok = await send_message_draft(bot, chat_id, draft_id, html_now, parse_mode="HTML")
            if not ok:
                drafts_supported = False
                break
            last_update = time.monotonic()
            buffer = ""

        if not drafts_supported:
            async for chunk in stream_response:
                if chunk.choices and chunk.choices[0].delta.content:
                    full_response += chunk.choices[0].delta.content

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning(f"Native draft stream interrupted: {e}")
        stream_error = e

    if full_response.strip():
        final_html = markdown_to_telegram_html(full_response)
        # Финальное сообщение без cancel-кнопки (запрос завершён)
        await send_long_text(msg, final_html, parse_mode="HTML")
    elif stream_error:
        raise stream_error

    return full_response


async def handle_streaming_response(
    msg: Message,
    stream_response,
    save_as_question: str,
    initial_message: Message | None = None,
    cancel_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Точка входа в стриминг + сохранение контекста.

    CancelledError ПРОБРАСЫВАЕТСЯ — её ловит process_content и редактирует
    loader в «Отменено». Здесь только нерекуррентные ошибки.
    """
    if USE_NATIVE_DRAFT_STREAM:
        full_response = await _stream_via_native_draft(
            msg, stream_response, initial_message, cancel_markup
        )
    else:
        full_response = await _stream_via_edit_text(
            msg, stream_response, initial_message, cancel_markup
        )

    if not full_response.strip():
        logger.error("Empty streaming response")
        if initial_message is not None:
            await safe_edit_text(
                initial_message,
                "❌ Произошла ошибка: ответ нейросети пустой.",
                parse_mode="HTML",
                reply_markup=None,
            )
        else:
            await safe_answer(msg, "Произошла ошибка: ответ нейросети пустой.")
        return

    try:
        await save_context(msg.from_user.id, save_as_question, full_response)
    except Exception as e:
        logger.error(f"Failed to save context: {e}")


# ────────────────────────────────────────────────────────────────────────────
# Основной пайплайн
# ────────────────────────────────────────────────────────────────────────────
async def _do_processing(
    msg: Message,
    content: str,
    image_paths: list[str] | None,
    loader: Message,
    cancel_markup: InlineKeyboardMarkup,
) -> None:
    """
    Та самая работа, которая может быть отменена. Запускается как Task,
    регистрируется в реестре отмены по (chat_id, loader.message_id).
    """
    has_images = bool(image_paths)
    user_id = msg.from_user.id

    try:
        async with log_timing("pipeline.analyze", user=user_id, has_images=has_images):
            wants_code, processed_content = await analyzer.analyze(content, image_paths)

        log_event(
            "pipeline.intent",
            user=user_id,
            intent="CODE" if wants_code else "TEXT",
            processed_chars=len(processed_content),
        )

        request_content = processed_content if has_images else content

        # Loader — переключаем на «готовлю ответ» (если был «распознаю»)
        if has_images:
            await safe_edit_text(
                loader,
                "✍️ <b>Готовлю ответ...</b>",
                parse_mode="HTML",
                reply_markup=cancel_markup,
            )

        if wants_code:
            with suppress(Exception):
                await msg.react([ReactionTypeEmoji(emoji=random.choice(POPULAR_EMOJIS))])

            stream = await generate_code(
                telegram_id=user_id,
                request=request_content,
                stream=True,
            )
        else:
            stream = await process_request(
                telegram_id=user_id,
                content=request_content,
                stream=True,
            )

        await handle_streaming_response(
            msg,
            stream,
            save_as_question=content,
            initial_message=loader,
            cancel_markup=cancel_markup,
        )

    except asyncio.CancelledError:
        # Обрабатываем здесь, чтобы пользователь увидел понятное сообщение.
        # НЕ пере-raise — задача завершается «успешно отменённой».
        log_event("pipeline.cancelled", user=user_id)
        with suppress(Exception):
            await safe_edit_text(
                loader,
                "⏹ <b>Запрос отменён</b>",
                parse_mode="HTML",
                reply_markup=None,
            )
    except ValueError as e:
        # Бизнес-ошибки (валидация изображений и т.п.)
        await safe_edit_text(loader, f"❌ {e}", parse_mode="HTML", reply_markup=None)
    except Exception as e:
        logger.exception(f"_do_processing failed: {e}")
        await safe_edit_text(
            loader,
            "❌ Произошла ошибка при обработке запроса. Попробуйте позже.",
            parse_mode="HTML",
            reply_markup=None,
        )


async def process_content(
    msg: Message,
    content: str,
    image_paths: list[str] | None = None,
) -> None:
    """Точка входа: проверки, lock, loader с кнопкой отмены, запуск задачи."""
    if not await check_subscription(msg):
        return

    if len(content.split()) > MAX_WORD_COUNT:
        await safe_answer(
            msg,
            "К сожалению, текст вашего сообщения слишком длинный. "
            "Сократите его, чтобы получить ответ нейросети.",
        )
        return

    user_id = msg.from_user.id
    has_images = bool(image_paths)

    log_event(
        "msg.received",
        user=user_id,
        chat=msg.chat.id,
        has_images=has_images,
        n_images=len(image_paths or []),
        chars=len(content),
    )

    async with user_lock(user_id) as acquired:
        if not acquired:
            await safe_answer(
                msg,
                "⏳ Я ещё обрабатываю ваше предыдущее сообщение. Дождитесь ответа, "
                "или нажмите «Отменить» под предыдущим сообщением.",
            )
            return

        # ─── Loader с кнопкой [Отменить] ───
        # Текст подбираем под тип входа.
        if has_images:
            initial_text = "🖼 <b>Распознаю изображение...</b>"
        else:
            initial_text = "💭 <b>Думаю...</b>"

        # Сначала отправляем БЕЗ кнопки, чтобы получить message_id;
        # потом вешаем кнопку с этим id в callback_data.
        loader = await safe_answer(msg, initial_text, parse_mode="HTML")
        if loader is None:
            logger.error(f"Could not send loader for user {user_id}")
            return

        cancel_markup = make_cancel_keyboard(loader.chat.id, loader.message_id)
        # Прицепляем кнопку
        await safe_edit_text(
            loader, initial_text, parse_mode="HTML", reply_markup=cancel_markup
        )

        # ─── Запускаем работу как Task, чтобы её можно было cancel() ───
        task = asyncio.create_task(
            _do_processing(msg, content, image_paths, loader, cancel_markup),
            name=f"process-{user_id}-{loader.message_id}",
        )
        register_task(loader.chat.id, loader.message_id, task)

        async with ChatActionSender.typing(chat_id=msg.chat.id, bot=bot):
            try:
                # await не должен падать, потому что _do_processing внутри ловит всё
                await task
            except asyncio.CancelledError:
                # На случай если cancel прилетел во внешнем await раньше внутреннего
                log_event("pipeline.outer_cancel", user=user_id)


# ────────────────────────────────────────────────────────────────────────────
# Хендлеры
# ────────────────────────────────────────────────────────────────────────────
@rt.message(F.text)
async def text_handler(msg: Message) -> None:
    try:
        await process_content(msg, msg.text)
    except Exception as e:
        logger.exception(f"text_handler error: {e}")
        await safe_answer(msg, lexicon["error_text"])


@rt.message(F.voice)
async def voice_handler(msg: Message) -> None:
    voice_path: Path | None = None
    try:
        if not await check_subscription(msg):
            return

        voice_path = DOCUMENTS_DIR / f"{msg.voice.file_id}.ogg"

        async with ChatActionSender(action=ChatAction.RECORD_VOICE, chat_id=msg.chat.id, bot=bot):
            async with log_timing("telegram.download_voice", file_id=msg.voice.file_id):
                buf = await bot.download(msg.voice.file_id)
                async with aiofiles.open(voice_path, "wb") as f:
                    await f.write(buf.read())

            text = await process_audio_with_whisper(
                telegram_id=msg.from_user.id,
                file_path=str(voice_path),
            )

        if not text or not text.strip():
            await safe_answer(msg, "Не удалось распознать речь. Попробуйте записать чётче.")
            return

        await process_content(msg, text)

    except Exception as e:
        logger.exception(f"voice_handler error: {e}")
        await safe_answer(msg, lexicon["error_voice"])
    finally:
        if voice_path and voice_path.exists():
            with suppress(OSError):
                voice_path.unlink()


@rt.message(F.document)
async def document_handler(msg: Message) -> None:
    file_path: Path | None = None
    try:
        if not await check_subscription(msg):
            return

        async with log_timing("telegram.get_file", file_id=msg.document.file_id):
            file = await bot.get_file(msg.document.file_id)
        remote_basename = os.path.basename(file.file_path)
        file_path = DOCUMENTS_DIR / f"{msg.document.file_id}_{remote_basename}"

        async with ChatActionSender(action=ChatAction.UPLOAD_DOCUMENT, chat_id=msg.chat.id, bot=bot):
            async with log_timing(
                "telegram.download_file",
                size=msg.document.file_size,
                name=msg.document.file_name,
            ):
                await bot.download_file(file.file_path, file_path)

            filename = (msg.document.file_name or "").lower()
            if filename.endswith(".pdf"):
                text = await read_pdf(file_path)
            elif filename.endswith(".docx"):
                text = await read_docx(file_path)
            elif filename.endswith(".txt"):
                text = await read_txt(file_path)
            else:
                await safe_answer(
                    msg,
                    "Поддерживаются только .pdf, .docx и .txt. "
                    "Скопируйте текст из файла и пришлите сообщением.",
                )
                return

        if not text or not text.strip():
            await safe_answer(msg, "Не удалось извлечь текст из документа.")
            return

        full_content = text
        if msg.caption:
            full_content = f"{text}\n\n{msg.caption}"

        await process_content(msg, full_content)

    except Exception as e:
        logger.exception(f"document_handler error for {msg.document.file_name}: {e}")
        await safe_answer(msg, lexicon["error_document"])
    finally:
        if file_path and file_path.exists():
            with suppress(OSError):
                file_path.unlink()


@rt.message(F.photo)
async def photo_handler(msg: Message) -> None:
    """Одиночные фото и альбомы (media_group)."""
    image_paths: list[str] = []
    try:
        if not await check_subscription(msg):
            return

        media_group_id = msg.media_group_id

        if media_group_id:
            key = f"album:{msg.from_user.id}:{media_group_id}"
            photo_info = json.dumps({
                "file_id": msg.photo[-1].file_id,
                "caption": msg.caption or "",
            })
            await redis.lpush(key, photo_info)

            is_leader = await redis.set(f"{key}:lock", b"1", ex=10, nx=True)
            if not is_leader:
                return

            await asyncio.sleep(1.2)

            async with redis.pipeline(transaction=True) as pipe:
                pipe.lrange(key, 0, -1)
                pipe.delete(key, f"{key}:lock")
                results = await pipe.execute()
            album_data = results[0]

            if len(album_data) > MAX_IMAGES_PER_REQUEST:
                await safe_answer(msg, f"❌ Максимум {MAX_IMAGES_PER_REQUEST} изображений за раз")
                return

            caption = ""

            async def _download(entry: bytes) -> str:
                photo = json.loads(entry.decode("utf-8"))
                nonlocal caption
                if photo["caption"] and not caption:
                    caption = photo["caption"]
                f = await bot.get_file(photo["file_id"])
                fp = DOCUMENTS_DIR / f"{photo['file_id']}.jpg"
                await bot.download_file(f.file_path, fp)
                return str(fp)

            async with log_timing("telegram.download_album", count=len(album_data)):
                image_paths = await asyncio.gather(*(_download(e) for e in album_data))

            content = caption or "Опиши что на изображениях. Если есть текст или задачи — извлеки их."
            await process_content(msg, content, image_paths=image_paths)

        else:
            f = await bot.get_file(msg.photo[-1].file_id)
            fp = DOCUMENTS_DIR / f"{msg.photo[-1].file_id}.jpg"
            async with log_timing("telegram.download_photo", file_id=msg.photo[-1].file_id):
                await bot.download_file(f.file_path, fp)
            image_paths.append(str(fp))

            content = msg.caption or "Опиши что на изображении. Если есть текст или задача — извлеки его полностью."
            await process_content(msg, content, image_paths=image_paths)

    except Exception as e:
        logger.exception(f"photo_handler error: {e}")
        await safe_answer(msg, "Произошла ошибка при обработке изображения.")
    finally:
        for p in image_paths:
            with suppress(OSError, FileNotFoundError):
                os.remove(p)


# ────────────────────────────────────────────────────────────────────────────
# Callback-хендлеры
# ────────────────────────────────────────────────────────────────────────────
@rt.callback_query(lambda c: c.data and c.data.startswith(CANCEL_CB_PREFIX))
async def cancel_callback(callback: CallbackQuery) -> None:
    """Обработка нажатия [Отменить] под сообщением бота."""
    parsed = parse_cancel_data(callback.data or "")
    if parsed is None:
        await callback.answer("Некорректные данные", show_alert=False)
        return

    chat_id, message_id = parsed
    log_event(
        "cancel.button_pressed",
        user=callback.from_user.id,
        chat=chat_id,
        message=message_id,
    )

    cancelled = cancel_task(chat_id, message_id)
    if cancelled:
        await callback.answer("⏹ Запрос отменяется...")
    else:
        await callback.answer("Запрос уже завершён", show_alert=False)
        # Если задачи нет — снимем кнопку, чтобы юзер её больше не видел
        if callback.message:
            with suppress(Exception):
                await callback.message.edit_reply_markup(reply_markup=None)


@rt.callback_query(lambda c: c.data == "check_subscription")
async def check_subscription_callback(callback: CallbackQuery) -> None:
    try:
        if await is_subscribed(callback.from_user.id):
            await callback.answer("✅ Спасибо за подписку! Доступ открыт.")
            with suppress(TelegramBadRequest):
                await callback.message.delete()
            await safe_answer(
                callback.message,
                "✅ Доступ к боту открыт! Теперь вы можете отправить мне свой запрос.",
            )
        else:
            await callback.answer(
                f"❌ Вы ещё не подписались на канал {CHANNEL_USERNAME}",
                show_alert=True,
            )
    except Exception as e:
        logger.exception(f"check_subscription_callback error: {e}")
        await callback.answer("Произошла ошибка при проверке подписки.", show_alert=True)
