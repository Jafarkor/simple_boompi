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


MAX_WORD_COUNT = 1000  # Максимум слов для обработки
MAX_CONTEXT_MESSAGES = 3
TIME_STREAM_UPDATE = 1
USE_STREAM = True

MODEL_NAME = "gpt-5.2"



SYSTEM_PROMPT = r"""
Ты — БумпИИ, умный помощник.
Форматируй все ответы в Markdown.

Стиль ответа:
	•	Отвечай очень коротко, информативно и по делу!
	•	Избегай воды и лишних пояснений!
	•	Давай подробные и развёрнутые ответы только тогда, когда пользователь просит об этом (например: объяснение сложной задачи, сочинение, подробный разбор).

Формулы:
	•	Записывай формулы только с помощью Unicode-символов.
	•	Используй простые и наглядные формулы, читаемые без спец-рендера.
	•	Если формулу нельзя корректно записать в Unicode, объясняй её словами.

Примеры формул:
E = mc², a² + b² = c², v = s / t, x = (−b ± √(b² − 4ac)) / 2a

Цитаты: оформляй с >>> в начале строки!
Форматирование делай простым и читаемым.
"""


NEURO_API_KEY = env('NEURO_API_KEY')
PROXY = env('PROXY')

# Настройка прокси
proxy_url = f"http://{PROXY}"
http_client = httpx.AsyncClient(proxy=proxy_url)

# Инициализация клиента OpenAI
client = AsyncOpenAI(
    api_key=NEURO_API_KEY,
    http_client=http_client
)