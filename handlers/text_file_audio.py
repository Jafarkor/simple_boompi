"""Обработчик text/voice/document/photo сообщений с устойчивым streaming."""
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
    FSInputFile,
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
from utils.code_generator import CodeGenerator
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
code_gen = CodeGenerator()


# ────────────────────────────────────────────────────────────────────────────
# Per-user lock — чтобы параллельные сообщения от одного пользователя не путались
# ────────────────────────────────────────────────────────────────────────────
@asynccontextmanager
async def user_lock(user_id: int):
    """
    SET NX EX в Redis. Если блокировка уже есть — пропускаем обработку
    (пользователю показываем мягкое сообщение в caller-е).
    """
    key = f"user:{user_id}:lock"
    acquired = await redis.set(key, b"1", ex=USER_LOCK_TTL, nx=True)
    if not acquired:
        yield False
        return
    try:
        yield True
    finally:
        with suppress(Exception):
            await redis.delete(key)


# ────────────────────────────────────────────────────────────────────────────
# Подписка на канал
# ────────────────────────────────────────────────────────────────────────────
async def is_subscribed(user_id: int) -> bool:
    try:
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
# Streaming — главное место, где раньше «обрывался ответ»
# ────────────────────────────────────────────────────────────────────────────
async def _stream_via_edit_text(
    msg: Message,
    stream_response,
) -> str:
    """
    Классический стриминг через sendMessage + editMessageText.
    Все вызовы Telegram идут через safe_edit_text/safe_answer —
    MessageNotModified и RetryAfter обрабатываются прозрачно.

    Возвращает полный собранный ответ (даже если последние правки в Telegram
    зафейлились — текст в любом случае не теряется).
    """
    full_response = ""
    buffer = ""
    sent_message: Message | None = None
    last_shown_html = ""
    last_update = time.monotonic() - TIME_STREAM_UPDATE  # чтобы первый чанк не ждал

    async for chunk in stream_response:
        # Отдельные чанки могут содержать только usage без content — пропускаем,
        # но НЕ через continue после проверки content (иначе можно проскочить контент).
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta.content
        if not delta:
            continue

        buffer += delta
        full_response += delta  # копим полный ответ независимо от рендера в TG

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
            sent_message = await safe_answer(msg, html_now, parse_mode="HTML")
            if sent_message is not None:
                last_shown_html = html_now
                last_update = time.monotonic()
        else:
            ok = await safe_edit_text(sent_message, html_now, parse_mode="HTML")
            if ok:
                last_shown_html = html_now
                last_update = time.monotonic()
            # если ok=False — просто продолжаем, в финале попытаемся ещё раз

        buffer = ""

    # Финальная отправка ВСЕГДА в try/except — это и был главный баг.
    # Теперь даже если последний edit упадёт с RetryAfter или MessageNotModified,
    # мы уже не пробросим исключение наружу.
    final_html = markdown_to_telegram_html(full_response)
    if not full_response.strip():
        return full_response  # ответ пустой — обработает caller

    if sent_message is None:
        # Стриминг закончился, не отправив ни одного апдейта (короткий ответ)
        await send_long_text(msg, final_html, parse_mode="HTML")
    elif final_html != last_shown_html:
        # Обновляем финальной версией — но НЕ падаем если не получилось
        ok = await safe_edit_text(sent_message, final_html, parse_mode="HTML")
        if not ok:
            logger.warning("Final edit failed — sending as a new message to ensure delivery")
            await send_long_text(msg, final_html, parse_mode="HTML")

    return full_response


async def _stream_via_native_draft(
    msg: Message,
    stream_response,
) -> str:
    """
    Native draft streaming через sendMessageDraft (Bot API 9.5+, март 2026).
    Без мерцания, без rate-limit-issues от edit_text.
    Если первый же запрос провалился — мгновенно падаем в edit_text fallback.
    """
    full_response = ""
    buffer = ""
    last_update = time.monotonic() - TIME_STREAM_UPDATE
    draft_id = random.randint(1, 2**31 - 1)
    chat_id = msg.chat.id
    drafts_supported = True

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
            or (elapsed >= 0.5 and len(buffer) >= STREAM_MIN_CHUNK_SIZE)  # native быстрее
        )
        if not ready or not drafts_supported:
            continue

        html_now = markdown_to_telegram_html(full_response)
        if not html_now.strip():
            buffer = ""
            continue

        ok = await send_message_draft(bot, chat_id, draft_id, html_now, parse_mode="HTML")
        if not ok:
            drafts_supported = False  # сервер не поддерживает — добиваем в edit_text
            break
        last_update = time.monotonic()
        buffer = ""

    if not drafts_supported:
        # Дочитываем оставшиеся чанки и переключаемся на edit_text
        async for chunk in stream_response:
            if chunk.choices and chunk.choices[0].delta.content:
                full_response += chunk.choices[0].delta.content

    # Финал — обычное сообщение, оно автоматически "коммитит" драфт у клиента
    if full_response.strip():
        final_html = markdown_to_telegram_html(full_response)
        await send_long_text(msg, final_html, parse_mode="HTML")

    return full_response


async def handle_streaming_response(
    msg: Message,
    stream_response,
    save_as_question: str,
) -> None:
    """Точка входа в стриминг: выбирает native/legacy режим и сохраняет контекст."""
    try:
        if USE_NATIVE_DRAFT_STREAM:
            full_response = await _stream_via_native_draft(msg, stream_response)
        else:
            full_response = await _stream_via_edit_text(msg, stream_response)
    except Exception as e:
        logger.exception(f"Streaming failed: {e}")
        await safe_answer(msg, "Произошла ошибка при получении ответа. Попробуйте ещё раз.")
        return

    if not full_response.strip():
        logger.error("Empty streaming response")
        await safe_answer(msg, "Произошла ошибка: ответ нейросети пустой.")
        return

    # Сохраняем контекст ОТДЕЛЬНО от показа в TG — чтобы даже при сбоях UI
    # история диалога не терялась
    try:
        await save_context(msg.from_user.id, save_as_question, full_response)
    except Exception as e:
        logger.error(f"Failed to save context: {e}")


# ────────────────────────────────────────────────────────────────────────────
# Файл с кодом
# ────────────────────────────────────────────────────────────────────────────
async def send_code_file(msg: Message, code_response: str, loader: Message | None) -> None:
    filepath, display_name = code_gen.create_file(code_response)
    try:
        if filepath is None:
            logger.warning("Could not extract code from response")
            if loader:
                with suppress(TelegramBadRequest):
                    await loader.delete()
            await safe_answer(msg, "❌ Не удалось извлечь код из ответа модели")
            return

        doc = FSInputFile(str(filepath), filename=display_name)
        if loader:
            with suppress(TelegramBadRequest):
                await loader.delete()
        await msg.answer_document(
            doc,
            caption='<b>Ваш код готов</b> <tg-emoji emoji-id="5208727996315220567">✅</tg-emoji>',
            parse_mode="HTML",
        )
    except Exception as e:
        logger.exception(f"Failed to send code file: {e}")
        await safe_answer(msg, "❌ Ошибка при отправке файла с кодом")
    finally:
        if filepath and filepath.exists():
            with suppress(OSError):
                filepath.unlink()


# ────────────────────────────────────────────────────────────────────────────
# Основной пайплайн обработки контента
# ────────────────────────────────────────────────────────────────────────────
async def process_content(
    msg: Message,
    content: str,
    image_paths: list[str] | None = None,
) -> None:
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

    async with user_lock(user_id) as acquired:
        if not acquired:
            await safe_answer(
                msg,
                "⏳ Я ещё обрабатываю ваше предыдущее сообщение. Дождитесь ответа, пожалуйста.",
            )
            return

        # Typing indicator всё время обработки — пользователь видит что бот работает
        async with ChatActionSender.typing(chat_id=msg.chat.id, bot=bot):
            try:
                wants_code, processed_content = await analyzer.analyze(content, image_paths)
                logger.info(f"User {user_id} wants {'CODE' if wants_code else 'TEXT'}")

                # Для текста без картинок — отдаём в модель оригинальный вопрос пользователя.
                # Если есть картинки — нужен processed (там описание картинок от анализатора).
                request_content = processed_content if image_paths else content

                if wants_code:
                    # Реакция-эмодзи — best effort, не падать если нет прав
                    with suppress(Exception):
                        await msg.react([ReactionTypeEmoji(emoji=random.choice(POPULAR_EMOJIS))])

                    loader = await safe_answer(
                        msg,
                        '<b>Генерация кода</b> <tg-emoji emoji-id="5339139919434498721">👾</tg-emoji>',
                        parse_mode="HTML",
                    )
                    response = await generate_code(
                        telegram_id=user_id,
                        request=request_content,
                        stream=False,
                    )
                    await send_code_file(msg, response, loader)
                else:
                    response = await process_request(
                        telegram_id=user_id,
                        content=request_content,
                        stream=USE_STREAM,
                    )
                    if USE_STREAM:
                        # Сохраняем в контекст ОРИГИНАЛЬНЫЙ вопрос пользователя
                        await handle_streaming_response(msg, response, save_as_question=content)
                    else:
                        await send_long_text(msg, markdown_to_telegram_html(response), parse_mode="HTML")

            except ValueError as e:
                await safe_answer(msg, f"❌ {e}")
            except Exception as e:
                logger.exception(f"process_content failed: {e}")
                await safe_answer(msg, "Произошла ошибка при обработке запроса. Попробуйте позже.")


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

        file = await bot.get_file(msg.document.file_id)
        # Защита от ошибочного парсинга пути из старого кода (split('/')[1] падал)
        remote_basename = os.path.basename(file.file_path)
        # Уникализируем имя — несколько одинаковых файлов одного юзера не перетрут друг друга
        file_path = DOCUMENTS_DIR / f"{msg.document.file_id}_{remote_basename}"

        async with ChatActionSender(action=ChatAction.UPLOAD_DOCUMENT, chat_id=msg.chat.id, bot=bot):
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
            # Альбом: первый обработчик собирает все фото, остальные выходят сразу
            key = f"album:{msg.from_user.id}:{media_group_id}"
            photo_info = json.dumps({
                "file_id": msg.photo[-1].file_id,
                "caption": msg.caption or "",
            })
            await redis.lpush(key, photo_info)

            # Лидер — тот, кто захватил блокировку
            is_leader = await redis.set(f"{key}:lock", b"1", ex=10, nx=True)
            if not is_leader:
                return

            # Ждём остальные фото (Telegram доставляет альбом в течение ~1 сек)
            await asyncio.sleep(1.2)

            # Атомарно забираем все накопленные фото и удаляем ключи
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

            # Скачиваем альбом параллельно — десятки секунд экономии
            image_paths = await asyncio.gather(*(_download(e) for e in album_data))

            content = caption or "Опиши что на изображениях. Если есть текст или задачи — извлеки их."
            await process_content(msg, content, image_paths=image_paths)

        else:
            # Одиночное фото
            f = await bot.get_file(msg.photo[-1].file_id)
            fp = DOCUMENTS_DIR / f"{msg.photo[-1].file_id}.jpg"
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
