import logging
import base64
import aiofiles
import os
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

Если пользователь хочет СГЕНЕРИРОВАТЬ КОД:
<intent>CODE</intent>
Опиши ПОДРОБНО что нужно сделать в коде, на каком языке, какие требования.
Если есть изображения с кодом - извлеки их содержимое.

Если пользователь хочет ОБЫЧНЫЙ ОТВЕТ (объяснение, решение задачи, описание изображения):
<intent>TEXT</intent>
Опиши что на изображениях (если есть), извлеки текст, задачи.
Или просто перескажи текстовый запрос для контекста.

Примеры:

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
Нужно решить квадратное уравнение x^2 + 5x + 6 = 0."""

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
            response = await self.client.chat.completions.create(
                model="meta-llama/llama-4-scout-17b-16e-instruct",
                messages=[
                    {"role": "system", "content": self.SYSTEM_PROMPT},
                    {"role": "user", "content": content}
                ],
                max_tokens=1024,
                temperature=0.8
            )

            result = response.choices[0].message.content.strip()

            # Парсим намерение
            if "<intent>CODE</intent>" in result:
                wants_code = True
                # Убираем тег из контента
                processed = result.replace("<intent>CODE</intent>", "").strip()
            elif "<intent>TEXT</intent>" in result:
                wants_code = False
                processed = result.replace("<intent>TEXT</intent>", "").strip()
            else:
                # Fallback: если тег не найден, пробуем определить по ключевым словам
                logger.warning(f"Intent tag not found in response: {result[:100]}")
                wants_code = any(word in result.lower() for word in
                               ["код", "code", "функция", "function", "скрипт", "script"])
                processed = result

            logger.info(f"Analysis result: wants_code={wants_code}, content_len={len(processed)}")
            return wants_code, processed

        except Exception as e:
            logger.error(f"Error in universal analysis: {e}")
            # Fallback: считаем что обычный запрос
            return False, user_text
