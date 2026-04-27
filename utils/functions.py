"""Утилиты для работы с файлами, OpenAI и Markdown→HTML конвертацией."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime
from html import escape
from io import BytesIO

import aiofiles
from PIL import Image
from pypdf import PdfReader
from docx import Document

from config.config import (
    redis,
    client,
    MAX_CONTEXT_MESSAGES,
    SYSTEM_PROMPT,
    CODE_GENERATION_PROMPT,
    MODEL_NAME,
    MAX_IMAGE_SIZE_MB,
    MAX_IMAGE_RESOLUTION_MP,
)

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────────────────
# Валидация изображений
# ────────────────────────────────────────────────────────────────────────────
async def validate_image(image_path: str) -> tuple[bool, str]:
    """Проверяет размер и разрешение. Не блокирует event loop."""
    try:
        file_size_mb = os.path.getsize(image_path) / (1024 * 1024)
        if file_size_mb > MAX_IMAGE_SIZE_MB:
            return False, (
                f"Размер изображения ({file_size_mb:.1f} MB) превышает "
                f"максимальный ({MAX_IMAGE_SIZE_MB} MB)"
            )

        async with aiofiles.open(image_path, "rb") as f:
            image_data = await f.read()

        # PIL — синхронный, выносим в thread pool
        def _check():
            with Image.open(BytesIO(image_data)) as img:
                w, h = img.size
                return (w * h) / 1_000_000

        megapixels = await asyncio.to_thread(_check)

        if megapixels > MAX_IMAGE_RESOLUTION_MP:
            return False, (
                f"Разрешение изображения ({megapixels:.1f} MP) превышает "
                f"максимальное ({MAX_IMAGE_RESOLUTION_MP} MP)"
            )

        return True, ""

    except Exception as e:
        logger.error(f"Ошибка валидации изображения {image_path}: {e}")
        return False, f"Ошибка проверки изображения: {e}"


# ────────────────────────────────────────────────────────────────────────────
# Whisper / транскрипция голосовых
# ────────────────────────────────────────────────────────────────────────────
async def process_audio_with_whisper(telegram_id: int, file_path: str) -> str:
    mp3_path = file_path.rsplit(".", 1)[0] + ".mp3"
    try:
        process = await asyncio.create_subprocess_exec(
            "/usr/bin/ffmpeg", "-y", "-i", file_path, "-acodec", "mp3", mp3_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {stderr.decode(errors='replace')[:500]}")

        async with aiofiles.open(mp3_path, "rb") as audio_file:
            audio_data = await audio_file.read()

        transcription = await client.audio.transcriptions.create(
            model="gpt-4o-mini-transcribe",
            file=("audio.mp3", audio_data, "audio/mp3"),
            response_format="text",
        )
        return transcription if isinstance(transcription, str) else str(transcription)

    finally:
        # Подчищаем mp3 даже при ошибке
        if os.path.exists(mp3_path):
            try:
                os.remove(mp3_path)
            except OSError:
                pass


# ────────────────────────────────────────────────────────────────────────────
# Markdown → Telegram HTML
# ────────────────────────────────────────────────────────────────────────────
_ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a", "blockquote"}


def markdown_to_telegram_html(text: str) -> str:
    """
    Конвертирует Markdown в безопасный для Telegram HTML.
    Гарантирует, что все теги сбалансированы — ключевое для стриминга,
    где отображаемый текст обрывается посреди тега.
    """
    text = escape(text)

    # Горизонтальная линия
    text = re.sub(r"^\s*---\s*$", "──────────────────", text, flags=re.MULTILINE)

    # >>> цитата (после escape стало &gt;&gt;&gt;)
    text = re.sub(
        r"(^|\n)\s*&gt;&gt;&gt;\s*(.*?)(?=\n|$)",
        r"\1<blockquote>\2</blockquote>",
        text,
        flags=re.MULTILINE,
    )

    # Заголовки # ## ### → <b>
    text = re.sub(r"^(#+)\s*(.*?)\s*$", r"<b>\2</b>", text, flags=re.MULTILINE)

    # Жирный
    text = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", text, flags=re.DOTALL)
    text = re.sub(r"__(.*?)__", r"<b>\1</b>", text, flags=re.DOTALL)

    # Курсив (одиночные * и _, но не при двойных)
    text = re.sub(r"(?<!\*)\*(?!\*)([^*\n]+?)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!_)_(?!_)([^_\n]+?)_(?!_)", r"<i>\1</i>", text)

    # Зачёркнутый
    text = re.sub(r"~~(.*?)~~", r"<s>\1</s>", text, flags=re.DOTALL)

    # Ссылки
    text = re.sub(r"\[(.*?)\]\((.*?)\)", r'<a href="\2">\1</a>', text)

    # Блок кода (тройные кавычки) — должен идти ДО однострочного `code`
    text = re.sub(r"```(?:\w*\n)?(.*?)```", r"<pre>\1</pre>", text, flags=re.DOTALL)

    # Однострочный код
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)

    # Очистим возможное вложение <b> вокруг <pre>/<blockquote>
    text = re.sub(r"<b>(<pre>.*?</pre>)</b>", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"<b>(<blockquote>.*?</blockquote>)</b>", r"\1", text, flags=re.DOTALL)

    return _validate_and_fix_html(text)


def _validate_and_fix_html(html: str) -> str:
    """
    Балансирует теги: открытые без закрытия — закрывает, лишние закрывающие — выкидывает.
    Это критично для стриминга, где сообщение обрывается посреди форматирования.
    """
    stack: list[str] = []
    result: list[str] = []
    parts = re.split(r"(</?[^>]+>)", html)

    for part in parts:
        if not part:
            continue

        if open_match := re.match(r"<(\w+)(?:\s[^>]*)?>$", part):
            tag = open_match.group(1).lower()
            if tag in _ALLOWED_TAGS:
                stack.append(tag)
                result.append(part)
            # неизвестные теги — игнорируем (могут прийти из бредового вывода модели)

        elif close_match := re.match(r"</(\w+)>$", part):
            tag = close_match.group(1).lower()
            if tag not in _ALLOWED_TAGS:
                continue
            if stack and stack[-1] == tag:
                stack.pop()
                result.append(part)
            elif tag in stack:
                # закрываем «через голову» — закрываем всё что выше по стеку
                while stack and stack[-1] != tag:
                    result.append(f"</{stack.pop()}>")
                if stack:
                    stack.pop()
                    result.append(part)
            # лишний закрывающий — игнор
        else:
            result.append(part)

    while stack:
        result.append(f"</{stack.pop()}>")
    return "".join(result)


# ────────────────────────────────────────────────────────────────────────────
# Контекст диалога в Redis
# ────────────────────────────────────────────────────────────────────────────
async def save_context(user_id: int, question: str, answer: str) -> None:
    if not isinstance(question, str) or not question.strip():
        logger.warning(f"Invalid question for user {user_id}")
        return
    if not isinstance(answer, str) or not answer.strip():
        logger.warning(f"Invalid answer for user {user_id}")
        return

    entry = json.dumps({"question": question, "answer": answer}, ensure_ascii=False)
    key = f"user:{user_id}:context"

    # Atomic с pipeline — три команды в одном round-trip
    async with redis.pipeline(transaction=False) as pipe:
        pipe.lpush(key, entry)
        pipe.ltrim(key, 0, MAX_CONTEXT_MESSAGES - 1)
        pipe.expire(key, 86400)
        await pipe.execute()

    logger.debug(f"Context saved for user {user_id}")


async def get_context(user_id: int) -> list[dict]:
    key = f"user:{user_id}:context"
    raw = await redis.lrange(key, 0, -1)
    if not raw:
        return []

    valid: list[dict] = []
    for entry in raw:
        try:
            ctx = json.loads(entry.decode("utf-8"))
            q, a = ctx.get("question"), ctx.get("answer")
            if isinstance(q, str) and q.strip() and isinstance(a, str) and a.strip():
                valid.append(ctx)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.warning(f"Failed to decode context for {user_id}: {e}")

    return valid


# ────────────────────────────────────────────────────────────────────────────
# Запросы к OpenAI
# ────────────────────────────────────────────────────────────────────────────
async def process_request(
    telegram_id: int,
    content: str,
    image_paths: list[str] | None = None,  # сохранён для совместимости, не используется
    stream: bool = False,
):
    """
    Запрос к основной модели. Если stream=True — возвращает async-итератор
    (контекст сохраняется снаружи, в handler-е, после полного получения).
    Если stream=False — возвращает строку и сохраняет контекст внутри.
    """
    context_list = await get_context(telegram_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for ctx in reversed(context_list):
        messages.append({"role": "user", "content": ctx["question"]})
        messages.append({"role": "assistant", "content": ctx["answer"]})
    messages.append({"role": "user", "content": content})

    if stream:
        return await client.chat.completions.create(
            messages=messages,
            model=MODEL_NAME,
            max_completion_tokens=1100,
            stream=True,
            stream_options={"include_usage": True},
        )

    response = await client.chat.completions.create(
        messages=messages,
        model=MODEL_NAME,
        max_completion_tokens=1100,
    )

    usage = response.usage
    if usage:
        logger.info(
            f"Tokens used: total={usage.total_tokens}, "
            f"in={usage.prompt_tokens}, out={usage.completion_tokens}"
        )

    bot_response = response.choices[0].message.content or ""
    if bot_response.strip():
        await save_context(telegram_id, content, bot_response)
    return bot_response


async def generate_code(telegram_id: int, request: str, stream: bool = False):
    """Запрос к модели с промптом для генерации кода."""
    context_list = await get_context(telegram_id)
    messages = [{"role": "system", "content": CODE_GENERATION_PROMPT}]
    for ctx in reversed(context_list):
        messages.append({"role": "user", "content": ctx["question"]})
        messages.append({"role": "assistant", "content": ctx["answer"]})
    messages.append({"role": "user", "content": request})

    if stream:
        return await client.chat.completions.create(
            messages=messages,
            model=MODEL_NAME,
            max_completion_tokens=2000,
            stream=True,
            stream_options={"include_usage": True},
        )

    response = await client.chat.completions.create(
        messages=messages,
        model=MODEL_NAME,
        max_completion_tokens=2000,
    )
    bot_response = response.choices[0].message.content or ""
    if bot_response.strip():
        await save_context(telegram_id, request, bot_response)
    return bot_response


# ────────────────────────────────────────────────────────────────────────────
# Чтение документов — все sync операции вынесены в thread pool,
# чтобы не блокировать event loop при больших PDF/DOCX.
# ────────────────────────────────────────────────────────────────────────────
async def read_pdf(file_path: str | os.PathLike) -> str:
    def _read() -> str:
        reader = PdfReader(str(file_path))
        return "\n".join((p.extract_text() or "") for p in reader.pages)
    return await asyncio.to_thread(_read)


async def read_docx(file_path: str | os.PathLike) -> str:
    def _read() -> str:
        doc = Document(str(file_path))
        return " ".join(p.text for p in doc.paragraphs if p.text)
    return await asyncio.to_thread(_read)


async def read_txt(file_path: str | os.PathLike) -> str:
    async with aiofiles.open(file_path, "r", encoding="utf-8", errors="replace") as f:
        return await f.read()


async def format_datetime(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y")
