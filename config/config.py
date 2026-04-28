"""Конфигурация бота: токены, клиенты, константы."""
import logging

from environs import Env
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis
from openai import AsyncOpenAI
import httpx


# ────────────────────────────────────────────────────────────────────────────
# ENV
# ────────────────────────────────────────────────────────────────────────────
env = Env()
env.read_env()

BOT_TOKEN: str = env("BOT_TOKEN")
NEURO_API_KEY: str = env("NEURO_API_KEY")
GROQ_API_KEY: str = env("GROQ_API_KEY")
PROXY: str = env("PROXY", default="")  # формат host:port или user:pass@host:port

# Опционально — секретный токен для верификации webhook (Telegram присылает его в заголовке)
WEBHOOK_SECRET: str = env("WEBHOOK_SECRET", default="")

REDIS_HOST: str = env("REDIS_HOST", default="redis")
REDIS_PORT: int = env.int("REDIS_PORT", default=6379)
REDIS_DB: int = env.int("REDIS_DB", default=0)


# ────────────────────────────────────────────────────────────────────────────
# AIOGRAM
# ────────────────────────────────────────────────────────────────────────────
# AiohttpSession — явная сессия, чтобы корректно закрыть на shutdown
session = AiohttpSession()

bot: Bot = Bot(
    token=BOT_TOKEN,
    session=session,
    default=DefaultBotProperties(
        parse_mode=ParseMode.HTML,
        # protect_content=False, link_preview_disabled=True и т.д. можно добавить здесь
    ),
)

# Redis для FSM и пользовательского контекста
redis: Redis = Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    db=REDIS_DB,
    decode_responses=False,  # храним bytes — в коде делаем .decode явно
)

storage = RedisStorage(redis=redis)
dp = Dispatcher(storage=storage)


# ────────────────────────────────────────────────────────────────────────────
# Боты, каналы, поддержка
# ────────────────────────────────────────────────────────────────────────────
CHANNEL_USERNAME = "@boompi_ai"
BOT_USERNAME = "@boompi_ai_bot"
SUPPORT_USERNAME = "@boompi_ai_support"


# ────────────────────────────────────────────────────────────────────────────
# Лимиты и режимы
# ────────────────────────────────────────────────────────────────────────────
MAX_WORD_COUNT = 3000
MAX_CONTEXT_MESSAGES = 7
MAX_TELEGRAM_MESSAGE_LENGTH = 4096  # лимит Telegram

# Сколько токенов разрешаем модели сгенерировать.
# 1100 было слишком мало — задачи с фото и сложные объяснения обрезались.
# 4000 ≈ один полный ответ Telegram (~4096 символов).
MAX_OUTPUT_TOKENS_TEXT = 4000
MAX_OUTPUT_TOKENS_CODE = 4000

# Streaming
USE_STREAM = True
TIME_STREAM_UPDATE = 1.2  # сек между edit_text — щадящий темп для Telegram
STREAM_MIN_CHUNK_SIZE = 50
STREAM_MAX_CHUNK_SIZE = 350

# Native draft streaming (Bot API 9.5+, март 2026) — отключено по умолчанию,
# т.к. требует свежего сервера Bot API. При проблемах сразу падает в fallback edit_text.
USE_NATIVE_DRAFT_STREAM = env.bool("USE_NATIVE_DRAFT_STREAM", default=False)

# Изображения
MAX_IMAGES_PER_REQUEST = 5
MAX_IMAGE_SIZE_MB = 4
MAX_IMAGE_RESOLUTION_MP = 33

# Per-user lock — сколько ждать перед тем как сказать «уже обрабатываю»
USER_LOCK_TTL = 120  # сек

# Модель основного провайдера
MODEL_NAME = "gpt-5.5"


# ────────────────────────────────────────────────────────────────────────────
# Промпты
# ────────────────────────────────────────────────────────────────────────────
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


# ────────────────────────────────────────────────────────────────────────────
# HTTP клиенты
# ────────────────────────────────────────────────────────────────────────────
# Основной клиент через прокси (переменная PROXY = "user:pass@host:port" или "host:port")
_proxy_url = f"http://{PROXY}" if PROXY else None

# Limits — пул соединений и таймауты, чтобы не плодить TCP коннекты.
# read=180 — стримы с картинками иногда долго стартуют (5-15с на vision) +
# само время генерации (до 60с на длинный ответ).
_limits = httpx.Limits(max_keepalive_connections=20, max_connections=100, keepalive_expiry=30.0)
_timeout = httpx.Timeout(connect=10.0, read=180.0, write=30.0, pool=10.0)

http_client_main = httpx.AsyncClient(proxy=_proxy_url, limits=_limits, timeout=_timeout)
http_client_groq = httpx.AsyncClient(limits=_limits, timeout=_timeout)

client = AsyncOpenAI(api_key=NEURO_API_KEY, http_client=http_client_main)
groq_client = AsyncOpenAI(
    base_url="https://api.groq.com/openai/v1",
    api_key=GROQ_API_KEY,
    http_client=http_client_groq,
)


async def shutdown_clients() -> None:
    """Корректно закрывает все ресурсы. Вызывается из main.on_shutdown."""
    logging.info("Closing HTTP clients and bot session...")
    try:
        await http_client_main.aclose()
    except Exception as e:
        logging.warning(f"Error closing main http client: {e}")
    try:
        await http_client_groq.aclose()
    except Exception as e:
        logging.warning(f"Error closing groq http client: {e}")
    try:
        await session.close()
    except Exception as e:
        logging.warning(f"Error closing bot session: {e}")
    try:
        await redis.aclose()
    except Exception as e:
        logging.warning(f"Error closing redis: {e}")
    logging.info("All resources closed.")
