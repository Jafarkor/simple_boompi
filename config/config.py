from environs import Env
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.redis import RedisStorage, Redis
from openai import AsyncOpenAI
import httpx


env = Env()
env.read_env()


BOT_TOKEN: str = env("BOT_TOKEN")
bot: Bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode='HTML'))
redis = Redis(host='redis')
storage = RedisStorage(redis=redis)
dp = Dispatcher(storage=storage)


CHANNEL_USERNAME = '@boompi_ai'
BOT_USERNAME = '@boompi_ai_bot'
SUPPORT_USERNAME = "@boompi_ai_support"


MAX_WORD_COUNT = 2000
MAX_CONTEXT_MESSAGES = 7
TIME_STREAM_UPDATE = 1
USE_STREAM = True

MODEL_NAME = "gpt-5.3-chat-latest"

# Ограничения для изображений
MAX_IMAGES_PER_REQUEST = 5
MAX_IMAGE_SIZE_MB = 4  # для base64
MAX_IMAGE_RESOLUTION_MP = 33  # мегапикселей


SYSTEM_PROMPT = r"""
Ты — БумпИИ, умный помощник.
Форматируй все ответы в Markdown.

Стиль ответа:
	•	Избегай воды и лишних пояснений.
	•	Давай подробные и развёрнутые ответы только тогда, когда это нужно (например: объяснение сложной задачи, сочинение, подробный разбор).

Формулы:
	•	Записывай формулы только с помощью Unicode-символов.
	•	Используй простые и наглядные формулы, читаемые без спец-рендера.
	•	Если формулу нельзя корректно записать в Unicode, объясняй её словами.

Примеры формул:
E = mc², a² + b² = c², v = s / t, x = (−b ± √(b² − 4ac)) / 2a

Цитаты: оформляй с >>> в начале строки!
Форматирование делай простым и читаемым.
"""


CODE_GENERATION_PROMPT = r"""
Ты — эксперт-программист. Генерируй чистый, рабочий код.

Правила:
1. Оборачивай код в ```язык и ``` (например ```python)
2. Пиши готовый к запуску код без объяснений до/после блока
3. Используй понятные имена переменных и функций
4. Добавляй краткие комментарии для сложной логики
5. Следуй best practices языка

Поддерживаемые языки: Python, JavaScript, TypeScript, Java, C++, Go, Rust, PHP и др.
"""


NEURO_API_KEY = env('NEURO_API_KEY')
GROQ_API_KEY = env('GROQ_API_KEY')
PROXY = env('PROXY')

# Настройка прокси для основного клиента
proxy_url = f"http://{PROXY}"
http_client = httpx.AsyncClient(proxy=proxy_url)

# Основной клиент OpenAI
client = AsyncOpenAI(
    api_key=NEURO_API_KEY,
    http_client=http_client
)

# Клиент Groq (универсальный анализатор - изображения + намерения)
groq_client = AsyncOpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY
)