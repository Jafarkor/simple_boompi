import aiofiles
import asyncio
from docx import Document
import PyPDF2
import json
from datetime import datetime
import base64
from config.config import (redis, client, vision_client, MAX_CONTEXT_MESSAGES,
                           SYSTEM_PROMPT, VISION_SYSTEM_PROMPT, MODEL_NAME,
                           VISION_MODEL_NAME, MAX_IMAGES_PER_REQUEST,
                           MAX_IMAGE_SIZE_MB, MAX_IMAGE_RESOLUTION_MP)
import logging
import re
import os
from html import escape
from PIL import Image
from io import BytesIO


logging.basicConfig(level=logging.INFO)


async def validate_image(image_path: str) -> tuple[bool, str]:
    """
    Проверяет изображение на соответствие ограничениям Groq API
    Возвращает (is_valid, error_message)
    """
    try:
        # Проверка размера файла
        file_size_mb = os.path.getsize(image_path) / (1024 * 1024)
        if file_size_mb > MAX_IMAGE_SIZE_MB:
            return False, f"Размер изображения ({file_size_mb:.1f}MB) превышает максимальный ({MAX_IMAGE_SIZE_MB}MB)"

        # Проверка разрешения
        async with aiofiles.open(image_path, "rb") as f:
            image_data = await f.read()

        img = Image.open(BytesIO(image_data))
        width, height = img.size
        megapixels = (width * height) / 1_000_000

        if megapixels > MAX_IMAGE_RESOLUTION_MP:
            return False, f"Разрешение изображения ({megapixels:.1f}MP) превышает максимальное ({MAX_IMAGE_RESOLUTION_MP}MP)"

        return True, ""

    except Exception as e:
        logging.error(f"Ошибка валидации изображения {image_path}: {e}")
        return False, f"Ошибка проверки изображения: {str(e)}"


async def process_images_with_groq(image_paths: list[str], user_prompt: str = None) -> str:
    """
    Обрабатывает изображения через Groq API
    Возвращает текстовое описание/извлечённый текст
    """
    try:
        if len(image_paths) > MAX_IMAGES_PER_REQUEST:
            raise ValueError(f"Максимум {MAX_IMAGES_PER_REQUEST} изображений за раз")

        # Валидация всех изображений
        for img_path in image_paths:
            is_valid, error_msg = await validate_image(img_path)
            if not is_valid:
                raise ValueError(error_msg)

        # Подготовка контента для запроса
        content = []

        # Добавляем текстовый промпт если есть
        if user_prompt:
            content.append({"type": "text", "text": user_prompt})
        else:
            default_prompt = "Опиши что на изображении. Если есть текст или задача - извлеки его полностью."
            content.append({"type": "text", "text": default_prompt})

        # Добавляем изображения
        for img_path in image_paths:
            async with aiofiles.open(img_path, "rb") as f:
                image_data = await f.read()

            img_base64 = base64.b64encode(image_data).decode("utf-8")

            # Определяем MIME-тип
            ext = img_path.lower().split('.')[-1]
            mime_mapping = {
                'jpg': 'image/jpeg',
                'jpeg': 'image/jpeg',
                'png': 'image/png',
                'webp': 'image/webp',
                'gif': 'image/gif'
            }
            mime_type = mime_mapping.get(ext, 'image/jpeg')

            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{img_base64}"
                }
            })

        # Запрос к Groq API
        messages = [
            {"role": "system", "content": VISION_SYSTEM_PROMPT},
            {"role": "user", "content": content}
        ]

        response = await vision_client.chat.completions.create(
            model=VISION_MODEL_NAME,
            messages=messages,
            max_tokens=2000,
            temperature=0.3
        )

        vision_result = response.choices[0].message.content
        logging.info(f"Groq vision response: {vision_result[:100]}...")

        return vision_result

    except Exception as e:
        logging.error(f"Ошибка при обработке изображений через Groq: {e}")
        raise


async def process_audio_with_whisper(telegram_id, file_path: str) -> str:
    try:
        mp3_path = file_path.replace(".ogg", ".mp3")
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/ffmpeg", "-i", file_path, "-acodec", "mp3", mp3_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            raise Exception(f"Ошибка конвертации аудио: {stderr.decode()}")

        async with aiofiles.open(mp3_path, "rb") as audio_file:
            audio_data = await audio_file.read()

        transcription = await client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=("audio.mp3", audio_data, "audio/mp3"),
            response_format="text",
        )

        os.remove(mp3_path)
        return transcription

    except Exception as e:
        logging.error(f"Ошибка при обработке аудио с gpt-4o-transcribe: {e}")
        raise


def is_simple_response(text: str) -> bool:
    if re.search(r'\$\$.*?\$\$|\$.*?\$', text):
        return False
    if re.search(r'\|.*?\|.*?\|-', text, re.DOTALL):
        return False
    return True


def markdown_to_telegram_html(text: str) -> str:
    text = escape(text)
    text = re.sub(r'^\s*---\s*$', r'──────────────────', text, flags=re.MULTILINE)
    text = re.sub(r'(^|\n)\s*&gt;&gt;&gt;\s*(.*?)(?=\n|$)', r'\1<blockquote>\2</blockquote>', text, flags=re.MULTILINE)
    text = re.sub(r'^(#+)\s*(.*?)\s*$', r'<b>\2</b>', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.*?)\*\*|__(.*?)__', r'<b>\1\2</b>', text)
    text = re.sub(r'\*(.*?)\*|_(.*?)_', r'<i>\1\2</i>', text)
    text = re.sub(r'__(.*?)__', r'<u>\1</u>', text)
    text = re.sub(r'~~(.*?)~~', r'<s>\1</s>', text)
    text = re.sub(r'\[(.*?)\]\((.*?)\)', r'<a href="\2">\1</a>', text)
    text = re.sub(r'`([^`\n]+)`', r'<code>\1</code>', text)
    text = re.sub(r'```(?:\w*\n)?(.*?)```', r'<pre>\1</pre>', text, flags=re.DOTALL)
    text = re.sub(r'<b>(<blockquote>.*?</blockquote>)</b>', r'\1', text)
    text = re.sub(r'<b>(<pre>.*?</pre>)</b>', r'\1', text)

    def validate_and_fix(html: str) -> str:
        allowed = {'b', 'i', 'u', 's', 'code', 'pre', 'a', 'blockquote'}
        stack = []
        result = []
        parts = re.split(r'(</?[^>]+>)', html)

        for part in parts:
            if not part:
                continue
            if match := re.match(r'<(\w+)(?:\s[^>]*)?>$', part):
                tag = match.group(1)
                if tag in allowed:
                    stack.append(tag)
                    result.append(part)
            elif match := re.match(r'</(\w+)>$', part):
                tag = match.group(1)
                if tag in allowed and stack and stack[-1] == tag:
                    stack.pop()
                    result.append(part)
                elif tag in allowed and tag in stack:
                    while stack and stack[-1] != tag:
                        result.append(f'</{stack.pop()}>')
                    if stack:
                        stack.pop()
                        result.append(part)
            else:
                result.append(part)

        while stack:
            result.append(f'</{stack.pop()}>')
        return ''.join(result)

    text = validate_and_fix(text)
    return text


async def format_datetime(dt: datetime) -> str:
    try:
        return dt.strftime('%d.%m.%Y')
    except ValueError as e:
        return f"Ошибка форматирования: {e}"


async def save_context(user_id: int, question: str, answer: str):
    """Сохраняет контекст диалога в Redis"""
    if not isinstance(question, str) or not question.strip():
        logging.warning(f"Invalid question for user {user_id}: {question}")
        return

    if not isinstance(answer, str) or not answer.strip():
        logging.warning(f"Invalid answer for user {user_id}: {answer}")
        return

    if isinstance(question, list):
        text_parts = [item.get("text", "") for item in question if isinstance(item, dict) and item.get("type") == "text"]
        question = " ".join(text_parts).strip()
        if not question:
            logging.warning(f"Empty question after list processing for user {user_id}")
            return

    context_entry = {"question": question, "answer": answer}
    key = f"user:{user_id}:context"
    await redis.lpush(key, json.dumps(context_entry, ensure_ascii=False))
    await redis.ltrim(key, 0, MAX_CONTEXT_MESSAGES - 1)
    await redis.expire(key, 86400)
    logging.info(f"Context saved for user {user_id}")


async def get_context(user_id: int) -> list:
    """Получает контекст диалога из Redis"""
    key = f"user:{user_id}:context"
    context_json_list = await redis.lrange(key, 0, -1)

    if not context_json_list:
        return []

    valid_context = []
    for entry in context_json_list:
        try:
            context = json.loads(entry.decode('utf-8'))
            question = context.get("question")
            answer = context.get("answer")

            if isinstance(question, str) and question.strip() and isinstance(answer, str) and answer.strip():
                valid_context.append(context)
            else:
                logging.warning(f"Invalid context entry for user {user_id}: question type={type(question)}, answer type={type(answer)}")

        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logging.error(f"Failed to decode context for user {user_id}: {e}")

    return valid_context


async def process_request(telegram_id: str, content: str = "Реши", image_paths: list[str] = None, stream: bool = False):
    """
    Обработка запроса с поддержкой изображений через Groq
    """
    context_list = await get_context(telegram_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    for context in reversed(context_list):
        messages.append({"role": "user", "content": context["question"]})
        messages.append({"role": "assistant", "content": context["answer"]})

    # Если есть изображения, сначала обрабатываем их через Groq
    if image_paths:
        try:
            vision_result = await process_images_with_groq(image_paths, content)

            # Формируем новый промпт с результатом анализа изображений
            enhanced_content = f"{content}\n\nИнформация с изображения:\n{vision_result}"
            user_message = {"role": "user", "content": enhanced_content}

        except Exception as e:
            logging.error(f"Ошибка обработки изображений: {e}")
            raise
    else:
        user_message = {"role": "user", "content": content}

    messages.append(user_message)

    if stream:
        response = await client.chat.completions.create(
            messages=messages,
            model=MODEL_NAME,
            max_completion_tokens=1100,
            stream=True,
            stream_options={"include_usage": True}
        )
        return response
    else:
        response = await client.chat.completions.create(
            messages=messages,
            model=MODEL_NAME,
            max_tokens=1100,
        )

        response_dict = json.loads(response.model_dump_json())
        usage = response_dict.get("usage", {})
        input_tokens = usage.get("prompt_tokens", 0)
        output_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", 0)

        logging.info(f"Токены: всего {total_tokens}, вход {input_tokens}, выход {output_tokens}")

        bot_response = response_dict.get("choices", [{}])[0].get("message", {}).get("content", "Ответ не найден")
        await save_context(telegram_id, user_message["content"], bot_response)
        return bot_response


def read_pdf(file_path):
    text = ""
    with open(file_path, "rb") as file:
        pdf_reader = PyPDF2.PdfReader(file)
        for page_num in range(len(pdf_reader.pages)):
            page = pdf_reader.pages[page_num]
            text += page.extract_text()
    return text


def read_docx(file_path):
    text = ""
    with open(file_path, "rb") as file:
        doc = Document(file)
        text = ' '.join([str(p.text) for p in doc.paragraphs if str(p.text)])
    return text


async def read_txt(file_path):
    async with aiofiles.open(file_path, "r", encoding="utf-8") as file:
        text = await file.read()
    return text