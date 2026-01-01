from utils.functions import (read_pdf, read_docx, read_txt, process_request,
                             markdown_to_telegram_html, process_audio_with_whisper,
                             save_context)
from lexicon.lexicon import LEXICON_RU as lexicon
from keyboards.keyboards import channel_subscription_keyboard
from aiogram import F, Router
from aiogram.types import Message, CallbackQuery
from aiogram.enums.chat_member_status import ChatMemberStatus
from pathlib import Path
import os
import asyncio
from config.config import MAX_WORD_COUNT, CHANNEL_USERNAME, USE_STREAM, TIME_STREAM_UPDATE, MAX_IMAGES_PER_REQUEST
import logging
import aiofiles

rt = Router()
logging.basicConfig(level=logging.INFO)


async def is_subscribed(user_id: int, bot) -> bool:
    """Проверяет подписку пользователя на канал"""
    try:
        member = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        logging.info(f"Subscription check for user {user_id}: status = {member.status}")
        return member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR,
                                ChatMemberStatus.CREATOR)
    except Exception as e:
        logging.error(f"Error checking subscription for user {user_id}: {e}")
        return False


async def check_subscription(msg: Message) -> bool:
    """Проверяет подписку и отправляет сообщение если не подписан"""
    if not await is_subscribed(msg.from_user.id, msg.bot):
        await msg.answer(
            "💙 Чтобы пользоваться ботом необходимо подписаться на наш официальный канал",
            reply_markup=channel_subscription_keyboard
        )
        return False
    return True


async def send_response(msg: Message, answer: str, existing_message=None):
    """Отправляет ответ пользователю с HTML форматированием"""
    html_answer = markdown_to_telegram_html(answer)

    if existing_message:
        await existing_message.edit_text(html_answer, parse_mode="HTML")
    else:
        await msg.answer(html_answer, parse_mode="HTML")


async def handle_streaming_response(msg: Message, stream_response, content: str):
    """Обрабатывает потоковый ответ от нейросети"""
    full_response = ""
    buffer = ""
    message = None

    MIN_UPDATE_INTERVAL = TIME_STREAM_UPDATE
    MIN_CHUNK_SIZE = 100
    MAX_CHUNK_SIZE = 200

    last_update_time = asyncio.get_event_loop().time() - MIN_UPDATE_INTERVAL

    async for chunk in stream_response:
        if hasattr(chunk, 'usage') and chunk.usage:
            continue

        neuro_content = chunk.choices[0].delta.content if chunk.choices else None
        if neuro_content is None:
            continue

        buffer += neuro_content
        current_time = asyncio.get_event_loop().time()
        time_since_update = current_time - last_update_time

        should_update = (len(buffer) >= MAX_CHUNK_SIZE or
                        (time_since_update >= MIN_UPDATE_INTERVAL and len(buffer) >= MIN_CHUNK_SIZE))

        if should_update:
            full_response += buffer
            html_answer = markdown_to_telegram_html(full_response)

            if html_answer.strip():
                try:
                    if message is None:
                        message = await msg.answer(html_answer, parse_mode="HTML")
                    else:
                        await message.edit_text(html_answer, parse_mode="HTML")
                    last_update_time = current_time
                except Exception as e:
                    logging.error(f"Failed to update message: {e}")

            buffer = ""

    if buffer:
        full_response += buffer

    if not full_response.strip():
        logging.error("Final streaming response is empty")
        await msg.answer("Произошла ошибка: ответ нейросети пустой.")
        return

    await save_context(msg.from_user.id, content, full_response)
    await send_response(msg, full_response, message)


async def process_content(msg: Message, content: str, image_paths: list[str] = None):
    """Общая функция обработки контента"""
    if not await check_subscription(msg):
        return

    if len(content.split()) > MAX_WORD_COUNT:
        await msg.answer("К сожалению, текст вашего сообщения слишком длинный. "
                        "Сократите его, чтобы получить ответ нейросети.")
        return

    try:
        response = await process_request(
            telegram_id=msg.from_user.id,
            image_paths=image_paths,
            content=content,
            stream=USE_STREAM
        )

        if USE_STREAM:
            await handle_streaming_response(msg, response, content)
        else:
            await send_response(msg, response)

    except ValueError as e:
        # Ошибки валидации изображений
        await msg.answer(f"❌ {str(e)}")
    except Exception as e:
        logging.error(f"Error processing content: {e}")
        await msg.answer("Произошла ошибка при обработке запроса. Попробуйте позже.")


@rt.message(F.text)
async def text_handler(msg: Message):
    """Обработчик текстовых сообщений"""
    try:
        await process_content(msg, msg.text)
    except Exception as e:
        logging.error(f"Error processing text message: {e}")
        await msg.answer(lexicon["error_text"])


@rt.message(F.voice)
async def voice_handler(msg: Message):
    """Обработчик голосовых сообщений"""
    try:
        if not await check_subscription(msg):
            return

        voice_path = f"documents/{msg.voice.file_id}.ogg"
        voice = await msg.bot.download(msg.voice.file_id)

        async with aiofiles.open(voice_path, "wb") as f:
            await f.write(voice.read())

        text = await process_audio_with_whisper(telegram_id=msg.from_user.id, file_path=voice_path)
        os.remove(voice_path)

        await process_content(msg, text)
    except Exception as e:
        logging.error(f"Error processing voice message: {e}")
        await msg.answer(lexicon["error_voice"])


@rt.message(F.document)
async def document_handler(msg: Message):
    """Обработчик документов"""
    try:
        if not await check_subscription(msg):
            return

        file = await msg.bot.get_file(msg.document.file_id)
        file_path = Path('documents') / file.file_path.split("/")[1]
        await msg.bot.download_file(file.file_path, file_path)

        filename = msg.document.file_name.lower()
        if filename.endswith('.pdf'):
            text = read_pdf(file_path)
        elif filename.endswith('.docx'):
            text = read_docx(file_path)
        elif filename.endswith('.txt'):
            text = await read_txt(file_path)
        else:
            await msg.answer(lexicon["error"])
            os.remove(file_path)
            return

        os.remove(file_path)

        if not text.strip():
            await msg.answer("Не удалось извлечь текст из документа.")
            return

        full_content = text + (f"\n{msg.caption}" if msg.caption else "")
        await process_content(msg, full_content)
    except Exception as e:
        logging.error(f"Error handling document {msg.document.file_name}: {e}")
        await msg.answer(lexicon["error_document"])


@rt.message(F.photo)
async def photo_handler(msg: Message):
    """Обработчик фотографий (одиночных и альбомов)"""
    try:
        if not await check_subscription(msg):
            return

        # Создаем директорию если не существует
        os.makedirs('documents', exist_ok=True)

        # Получаем media_group_id для определения альбома
        media_group_id = msg.media_group_id

        if media_group_id:
            # Это часть альбома - сохраняем во временное хранилище
            key = f"album:{msg.from_user.id}:{media_group_id}"

            # Используем Redis для временного хранения
            from config.config import redis
            import json

            # Сохраняем информацию о фото
            photo_info = {
                'file_id': msg.photo[-1].file_id,
                'caption': msg.caption or ""
            }

            await redis.lpush(key, json.dumps(photo_info))
            await redis.expire(key, 10)  # Храним 10 секунд

            # Ждем немного, чтобы собрать все фото из альбома
            await asyncio.sleep(0.5)

            # Проверяем количество собранных фото
            album_data = await redis.lrange(key, 0, -1)

            if len(album_data) > MAX_IMAGES_PER_REQUEST:
                await msg.answer(f"❌ Максимум {MAX_IMAGES_PER_REQUEST} изображений за раз")
                await redis.delete(key)
                return

            # Обрабатываем только первое сообщение из альбома
            album_size = await redis.llen(key)
            if album_size == len(album_data):
                # Скачиваем все фото
                image_paths = []
                caption = ""

                for data in album_data:
                    photo_data = json.loads(data.decode('utf-8'))
                    file_id = photo_data['file_id']
                    if photo_data['caption']:
                        caption = photo_data['caption']

                    file = await msg.bot.get_file(file_id)
                    # Используем file_id для уникального имени файла
                    file_path = f'documents/{file_id}.jpg'
                    await msg.bot.download_file(file.file_path, file_path)
                    image_paths.append(file_path)

                # Удаляем ключ из Redis
                await redis.delete(key)

                # Обрабатываем альбом
                content = caption or "Опиши что на изображениях. Если есть текст или задачи - извлеки их."

                try:
                    await process_content(msg, content, image_paths=image_paths)
                finally:
                    # Удаляем файлы
                    for path in image_paths:
                        if os.path.exists(path):
                            os.remove(path)
        else:
            # Одиночное фото
            file = await msg.bot.get_file(msg.photo[-1].file_id)
            # Используем file_id для создания уникального имени файла
            file_path = f'documents/{msg.photo[-1].file_id}.jpg'
            await msg.bot.download_file(file.file_path, file_path)

            content = msg.caption or "Опиши что на изображении. Если есть текст или задача - извлеки его полностью."

            try:
                await process_content(msg, content, image_paths=[file_path])
            finally:
                if os.path.exists(file_path):
                    os.remove(file_path)

    except Exception as e:
        logging.error(f"Error processing photo: {e}")
        await msg.answer("Произошла ошибка при обработке изображения.")


@rt.callback_query(lambda c: c.data == "check_subscription")
async def check_subscription_callback(callback: CallbackQuery):
    """Обработчик проверки подписки"""
    try:
        if await is_subscribed(callback.from_user.id, callback.bot):
            await callback.answer("✅ Спасибо за подписку! Теперь у вас есть доступ к боту.")
            await callback.message.delete()
            await callback.message.answer("✅ Доступ к боту открыт! Теперь вы можете отправить мне свой запрос.")
        else:
            await callback.answer(f"❌ Вы еще не подписались на канал {CHANNEL_USERNAME}", show_alert=True)
    except Exception as e:
        logging.error(f"Error in subscription check: {e}")
        await callback.answer("Произошла ошибка при проверке подписки.", show_alert=True)