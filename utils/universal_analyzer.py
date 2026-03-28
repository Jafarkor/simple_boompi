import logging
import base64
import aiofiles
import os
import re
from typing import Optional, Tuple, List
from openai import AsyncOpenAI
from PIL import Image
from io import BytesIO

logger = logging.getLogger(__name__)


class UniversalAnalyzer:
    """Анализирует текст/изображения и определяет намерение за ОДИН запрос к Groq"""

    # Ограничения для изображений (из config)
    MAX_IMAGES_PER_REQUEST = 5
    MAX_IMAGE_SIZE_MB = 4
    MAX_IMAGE_RESOLUTION_MP = 33

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
- "Напиши план на день" → TEXT
- "Составь список покупок" → TEXT
- "Что на этой картинке?" + [фото] → TEXT
- "Как функция sin(x) связана с косинусом?" → TEXT
- "Напиши поздравление на день рождения" → TEXT
- "Сделай краткое содержание книги" → TEXT
- "Переведи песню" → TEXT

Запрос: "Напиши калькулятор на Python"
<intent>CODE</intent>
Нужно создать калькулятор на языке Python с базовыми операциями: сложение, вычитание, умножение, деление.

Запрос: [фото с Python кодом] + "Исправь ошибки"
<intent>CODE</intent>
На изображении код Python: [код]. Нужно исправить синтаксические ошибки и улучшить код.

Запрос: "Что на этой картинке?" + [фото гор]
<intent>TEXT</intent>
На изображении горный пейзаж: заснеженные вершины, голубое небо, сосновый лес у подножия.

Запрос: "Реши уравнение x^2 + 5x + 6 = 0"
<intent>TEXT</intent>
Нужно решить квадратное уравнение x^2 + 5x + 6 = 0.

Запрос: "Напиши сочинение о временах года"
<intent>TEXT</intent>
Пользователь хочет получить сочинение (творческий текст) о временах года.

Запрос: "Напиши эссе про влияние технологий на общество"
<intent>TEXT</intent>
Пользователь хочет эссе — развёрнутый текстовый ответ про влияние технологий."""

    def __init__(self, groq_client: AsyncOpenAI):
        self.client = groq_client

    async def _validate_image(self, image_path: str) -> Tuple[bool, str]:
        """Валидация изображения"""
        try:
            file_size_mb = os.path.getsize(image_path) / (1024 * 1024)
            if file_size_mb > self.MAX_IMAGE_SIZE_MB:
                return False, f"Размер изображения ({file_size_mb:.1f}MB) превышает максимальный ({self.MAX_IMAGE_SIZE_MB}MB)"

            async with aiofiles.open(image_path, "rb") as f:
                image_data = await f.read()

            img = Image.open(BytesIO(image_data))
            width, height = img.size
            megapixels = (width * height) / 1_000_000

            if megapixels > self.MAX_IMAGE_RESOLUTION_MP:
                return False, f"Разрешение изображения ({megapixels:.1f}MP) превышает максимальное ({self.MAX_IMAGE_RESOLUTION_MP}MP)"

            return True, ""
        except Exception as e:
            logger.error(f"Ошибка валидации изображения {image_path}: {e}")
            return False, f"Ошибка проверки изображения: {str(e)}"

    async def _encode_image(self, image_path: str) -> Tuple[str, str]:
        """Кодирует изображение в base64"""
        async with aiofiles.open(image_path, "rb") as f:
            image_data = await f.read()

        img_base64 = base64.b64encode(image_data).decode("utf-8")

        ext = image_path.lower().split('.')[-1]
        mime_mapping = {
            'jpg': 'image/jpeg', 'jpeg': 'image/jpeg',
            'png': 'image/png', 'webp': 'image/webp', 'gif': 'image/gif'
        }
        mime_type = mime_mapping.get(ext, 'image/jpeg')

        return f"data:{mime_type};base64,{img_base64}", mime_type

    def _parse_intent(self, result: str) -> Tuple[Optional[bool], str]:
        """
        Надёжно парсит тег <intent> из ответа модели.
        Ищет тег только в начале строки (первые 200 символов), чтобы
        избежать ложных срабатываний при упоминании тега в теле ответа.

        Returns:
            (wants_code: Optional[bool], processed: str)
            wants_code = None если тег не найден
        """
        # Ищем тег в начале ответа (первые 200 символов)
        header = result[:200]

        code_match = re.search(r'<intent>CODE</intent>', header, re.IGNORECASE)
        text_match = re.search(r'<intent>TEXT</intent>', header, re.IGNORECASE)

        if code_match and text_match:
            # Оба тега — берём тот, что стоит раньше
            if code_match.start() < text_match.start():
                wants_code = True
                tag = code_match.group(0)
            else:
                wants_code = False
                tag = text_match.group(0)
        elif code_match:
            wants_code = True
            tag = code_match.group(0)
        elif text_match:
            wants_code = False
            tag = text_match.group(0)
        else:
            # Проверяем весь текст — вдруг модель добавила тег не в начале
            code_match_full = re.search(r'<intent>CODE</intent>', result, re.IGNORECASE)
            text_match_full = re.search(r'<intent>TEXT</intent>', result, re.IGNORECASE)

            if code_match_full and not text_match_full:
                wants_code = True
                tag = code_match_full.group(0)
            elif text_match_full and not code_match_full:
                wants_code = False
                tag = text_match_full.group(0)
            else:
                return None, result

        processed = result.replace(tag, "", 1).strip()
        return wants_code, processed

    def _fallback_intent(self, user_text: str) -> bool:
        """
        Fallback-определение намерения по ключевым словам в ОРИГИНАЛЬНОМ запросе
        пользователя (не в ответе модели).

        Возвращает True только если запрос явно связан с кодом/программированием.
        В сомнительных случаях — False (текстовый ответ безопаснее).
        """
        text_lower = user_text.lower()

        # Явные признаки запроса кода — специфичные фразы
        code_keywords = [
            "напиши код", "write code", "напиши скрипт", "напиши программу",
            "напиши функцию", "напиши класс", "напиши метод",
            "сделай сайт", "сделай бота", "сделай приложение",
            "создай сайт", "создай бота", "создай приложение", "создай скрипт",
            "on python", "на python", "на javascript", "на java", "на c++",
            "на c#", "на php", "на golang", "на rust", "на typescript",
            "html код", "css код", "исправь код", "отладь код",
            "дебаг", "debug", "рефакторинг", "рефакторить",
        ]

        for keyword in code_keywords:
            if keyword in text_lower:
                logger.info(f"Fallback: CODE detected by keyword '{keyword}'")
                return True

        # Всё остальное — текстовый ответ (безопасный дефолт)
        logger.info("Fallback: defaulting to TEXT (safe default)")
        return False

    async def analyze(
        self,
        user_text: str,
        image_paths: Optional[List[str]] = None
    ) -> Tuple[bool, str]:
        """
        Анализирует контент за ОДИН запрос к Groq

        Returns:
            (wants_code: bool, processed_content: str)
        """
        try:
            # Валидация изображений если есть
            if image_paths:
                if len(image_paths) > self.MAX_IMAGES_PER_REQUEST:
                    raise ValueError(f"Максимум {self.MAX_IMAGES_PER_REQUEST} изображений за раз")

                for img_path in image_paths:
                    is_valid, error_msg = await self._validate_image(img_path)
                    if not is_valid:
                        raise ValueError(error_msg)

            # Формируем контент для запроса
            content = [{"type": "text", "text": user_text}]

            # Добавляем изображения если есть
            if image_paths:
                for img_path in image_paths:
                    img_url, _ = await self._encode_image(img_path)
                    content.append({
                        "type": "image_url",
                        "image_url": {"url": img_url}
                    })

            # ОДИН запрос к Groq для всего
            # Температура 0.1 — для надёжной классификации (низкая = стабильный формат)
            response = await self.client.chat.completions.create(
                model="meta-llama/llama-4-scout-17b-16e-instruct",
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": content}
                ],
                max_tokens=1024,
                temperature=0.3
            )

            result = response.choices[0].message.content.strip()
            logger.debug(f"Analyzer raw response (first 200): {result[:200]}")

            # Надёжный парсинг тега
            wants_code, processed = self._parse_intent(result)

            if wants_code is None:
                # Тег не найден вообще — используем fallback по оригинальному запросу
                logger.warning(
                    f"Intent tag not found in response. Falling back to keyword check. "
                    f"Response start: {result[:100]!r}"
                )
                wants_code = self._fallback_intent(user_text)
                processed = result

            logger.info(f"Analysis result: wants_code={wants_code}, content_len={len(processed)}")
            return wants_code, processed

        except ValueError:
            # Пробрасываем ошибки валидации (лимиты изображений и т.п.)
            raise
        except Exception as e:
            logger.error(f"Error in universal analysis: {e}")
            # Безопасный fallback — пробуем определить по ключевым словам
            wants_code = self._fallback_intent(user_text)
            logger.info(f"Fallback after exception: wants_code={wants_code}")
            return wants_code, user_text