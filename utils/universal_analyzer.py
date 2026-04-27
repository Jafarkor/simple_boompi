"""Универсальный анализатор: за один запрос к Groq определяет CODE/TEXT и обрабатывает изображения."""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
from io import BytesIO
from typing import Optional, Tuple, List

import aiofiles
from PIL import Image
from openai import AsyncOpenAI

from config.config import (
    MAX_IMAGES_PER_REQUEST,
    MAX_IMAGE_SIZE_MB,
    MAX_IMAGE_RESOLUTION_MP,
)

logger = logging.getLogger(__name__)


_INTENT_TAG_RE = re.compile(r"<intent>\s*(CODE|TEXT)\s*</intent>", re.IGNORECASE)


class UniversalAnalyzer:
    """Один запрос к Groq: text + images → (wants_code, processed_content)."""

    SYSTEM_PROMPT = """Ты умный ассистент-анализатор. Анализируй запросы пользователя и определяй его намерение.

ВАЖНО: В своём ответе ты ОБЯЗАТЕЛЬНО начинаешь с тега <intent>CODE</intent> или <intent>TEXT</intent>

Правила определения:
- <intent>CODE</intent> — пользователь хочет получить ПРОГРАММНЫЙ КОД (скрипт, функцию, приложение, сайт, бот, алгоритм на языке программирования)
- <intent>TEXT</intent> — ВСЁ ОСТАЛЬНОЕ: объяснения, сочинения, эссе, рассказы, стихи, переводы, решения задач, описания, инструкции, вопросы, анализ изображений

Если пользователь хочет СГЕНЕРИРОВАТЬ КОД:
<intent>CODE</intent>
Опиши ПОДРОБНО что нужно сделать в коде, на каком языке, какие требования.
Если есть изображения с кодом - извлеки их содержимое.

Если пользователь хочет ОБЫЧНЫЙ ОТВЕТ (текст, объяснение, творческое задание и т.д.):
<intent>TEXT</intent>
Опиши что на изображениях (если есть), извлеки текст, задачи.
Или просто перескажи текстовый запрос для контекста.

Примеры CODE запросов (только программирование):
- "Напиши калькулятор на Python" → CODE
- "Сделай сайт на HTML и CSS" → CODE
- "Создай телеграм бота" → CODE
- "Напиши функцию сортировки на JavaScript" → CODE
- "Исправь ошибки в этом коде: [код]" → CODE
- [фото с кодом] + "Что не так с этим кодом?" → CODE

Примеры TEXT запросов (всё остальное):
- "Напиши сочинение о природе" → TEXT
- "Напиши эссе про космос" → TEXT
- "Напиши рассказ о дружбе" → TEXT
- "Переведи этот текст на английский" → TEXT
- "Объясни как работает интернет" → TEXT
- "Реши уравнение x^2 + 5x + 6 = 0" → TEXT
- "Что такое машинное обучение?" → TEXT
- "Что на этой картинке?" + [фото] → TEXT
- "Напиши поздравление на день рождения" → TEXT"""

    def __init__(self, groq_client: AsyncOpenAI):
        self.client = groq_client

    async def _validate_image(self, image_path: str) -> Tuple[bool, str]:
        try:
            file_size_mb = os.path.getsize(image_path) / (1024 * 1024)
            if file_size_mb > MAX_IMAGE_SIZE_MB:
                return False, f"Размер ({file_size_mb:.1f} MB) превышает {MAX_IMAGE_SIZE_MB} MB"

            async with aiofiles.open(image_path, "rb") as f:
                data = await f.read()

            def _mp() -> float:
                with Image.open(BytesIO(data)) as img:
                    w, h = img.size
                    return (w * h) / 1_000_000

            megapixels = await asyncio.to_thread(_mp)
            if megapixels > MAX_IMAGE_RESOLUTION_MP:
                return False, f"Разрешение ({megapixels:.1f} MP) превышает {MAX_IMAGE_RESOLUTION_MP} MP"

            return True, ""
        except Exception as e:
            logger.error(f"Ошибка валидации {image_path}: {e}")
            return False, f"Не удалось проверить изображение: {e}"

    async def _encode_image(self, image_path: str) -> str:
        async with aiofiles.open(image_path, "rb") as f:
            data = await f.read()
        b64 = base64.b64encode(data).decode("utf-8")
        ext = image_path.lower().rsplit(".", 1)[-1]
        mime = {
            "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "png": "image/png", "webp": "image/webp", "gif": "image/gif",
        }.get(ext, "image/jpeg")
        return f"data:{mime};base64,{b64}"

    def _parse_intent(self, result: str) -> Tuple[Optional[bool], str]:
        """Ищет тег в начале ответа; единый regex для обоих вариантов."""
        # Проверяем первые 200 символов — там должен быть тег
        match = _INTENT_TAG_RE.search(result, 0, 200)
        if match is None:
            # Может быть и где-то дальше, поищем во всём тексте
            match = _INTENT_TAG_RE.search(result)
        if match is None:
            return None, result

        wants_code = match.group(1).upper() == "CODE"
        processed = (result[:match.start()] + result[match.end():]).strip()
        return wants_code, processed

    def _fallback_intent(self, user_text: str) -> bool:
        """Безопасный fallback по ключевым словам в запросе пользователя."""
        text_lower = user_text.lower()
        keywords = [
            "напиши код", "write code", "напиши скрипт", "напиши программу",
            "напиши функцию", "напиши класс", "напиши метод",
            "сделай сайт", "сделай бота", "сделай приложение",
            "создай сайт", "создай бота", "создай приложение", "создай скрипт",
            "на python", "на javascript", "на java", "на c++",
            "на c#", "на php", "на golang", "на rust", "на typescript",
            "html код", "css код", "исправь код", "отладь код",
            "debug", "рефакторинг",
        ]
        for kw in keywords:
            if kw in text_lower:
                logger.info(f"Fallback: CODE detected by '{kw}'")
                return True
        logger.info("Fallback: defaulting to TEXT")
        return False

    async def analyze(
        self,
        user_text: str,
        image_paths: Optional[List[str]] = None,
    ) -> Tuple[bool, str]:
        """Один запрос к Groq → (wants_code, processed_content)."""
        try:
            if image_paths:
                if len(image_paths) > MAX_IMAGES_PER_REQUEST:
                    raise ValueError(f"Максимум {MAX_IMAGES_PER_REQUEST} изображений за раз")

                # Валидируем параллельно — экономит секунды на больших альбомах
                results = await asyncio.gather(
                    *(self._validate_image(p) for p in image_paths),
                    return_exceptions=True,
                )
                for ok, err in results:
                    if isinstance(ok, Exception):
                        raise ValueError(f"Ошибка проверки изображения: {ok}")
                    if not ok:
                        raise ValueError(err)

            # Формируем мультимодальный контент
            content: list[dict] = [{"type": "text", "text": user_text}]
            if image_paths:
                # Кодируем параллельно
                urls = await asyncio.gather(*(self._encode_image(p) for p in image_paths))
                for url in urls:
                    content.append({"type": "image_url", "image_url": {"url": url}})

            response = await self.client.chat.completions.create(
                model="meta-llama/llama-4-scout-17b-16e-instruct",
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": content},
                ],
                max_tokens=1024,
                temperature=0.3,
            )

            result = (response.choices[0].message.content or "").strip()
            logger.debug(f"Analyzer response (head): {result[:200]!r}")

            wants_code, processed = self._parse_intent(result)
            if wants_code is None:
                logger.warning(f"Intent tag not found, fallback. Head: {result[:100]!r}")
                wants_code = self._fallback_intent(user_text)
                processed = result or user_text

            logger.info(f"Analysis: wants_code={wants_code}, processed_len={len(processed)}")
            return wants_code, processed

        except ValueError:
            raise
        except Exception as e:
            logger.error(f"Analyzer error: {e}")
            return self._fallback_intent(user_text), user_text
