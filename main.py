"""Точка входа: aiohttp webhook сервер для aiogram-бота."""
import asyncio
import logging
import os

from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler

from config.config import bot, dp, shutdown_clients, WEBHOOK_SECRET
from handlers import final, general, text_file_audio
from keyboards.set_menu import set_main_menu
from middlewares.middlewares import GeneralMiddleware

# uvloop ускоряет asyncio в 2-4 раза на Linux. Если нет — игнор.
try:
    import uvloop  # type: ignore
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
    _UVLOOP = True
except ImportError:
    _UVLOOP = False


# ────────────────────────────────────────────────────────────────────────────
# Логирование
# ────────────────────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Снижаем шум сторонних либ
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)


# ────────────────────────────────────────────────────────────────────────────
# Webhook
# ────────────────────────────────────────────────────────────────────────────
WEBHOOK_HOST = os.environ.get("WEBHOOK_HOST", "https://boompiai.ru")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

WEBAPP_HOST = os.environ.get("WEBAPP_HOST", "0.0.0.0")
WEBAPP_PORT = int(os.environ.get("WEBAPP_PORT", "8080"))


async def on_startup(app: web.Application) -> None:
    logging.info(f"Starting bot (uvloop={_UVLOOP})")
    try:
        await set_main_menu()
        logging.info("Main menu set")

        # Даём nginx и сертам подняться
        await asyncio.sleep(2)

        webhook_info = await bot.get_webhook_info()
        logging.info(f"Current webhook: {webhook_info.url!r}")

        # set_webhook идемпотентен по url, но мы ещё проверяем secret_token
        # на изменение, поэтому всегда переустанавливаем при первом запуске
        kwargs = {
            "url": WEBHOOK_URL,
            "drop_pending_updates": True,
            "allowed_updates": dp.resolve_used_update_types(),
        }
        if WEBHOOK_SECRET:
            kwargs["secret_token"] = WEBHOOK_SECRET

        ok = await bot.set_webhook(**kwargs)
        if ok:
            logging.info(f"✅ Webhook set: {WEBHOOK_URL}")
            verify = await bot.get_webhook_info()
            logging.info(
                f"Verified — pending: {verify.pending_update_count}, "
                f"last error: {verify.last_error_message!r}"
            )
        else:
            logging.error("❌ set_webhook returned False")

    except Exception as e:
        logging.exception(f"on_startup error: {e}")


async def on_shutdown(app: web.Application) -> None:
    logging.info("Shutting down bot")
    try:
        await bot.delete_webhook(drop_pending_updates=False)
    except Exception as e:
        logging.warning(f"delete_webhook on shutdown: {e}")
    await shutdown_clients()


async def health_check(_request: web.Request) -> web.Response:
    """Простой health check для nginx/Docker."""
    try:
        # Проверяем что бот живой
        await bot.get_me()
        return web.Response(text="OK")
    except Exception as e:
        logging.warning(f"health_check failed: {e}")
        return web.Response(text="DEGRADED", status=503)


def main() -> None:
    # Middlewares и роутеры
    dp.update.middleware(GeneralMiddleware())
    dp.include_router(general.rt)
    dp.include_router(text_file_audio.rt)
    dp.include_router(final.rt)
    logging.info("Routers registered")

    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    # handle_in_background=True (default) — Telegram сразу получает 200,
    # обработка идёт фоном. Это и так дефолт в aiogram 3.25+, явно фиксируем.
    webhook_handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        handle_in_background=True,
        secret_token=WEBHOOK_SECRET or None,
    )
    webhook_handler.register(app, path=WEBHOOK_PATH)

    app.router.add_get("/health", health_check)

    logging.info(f"Starting webhook server on {WEBAPP_HOST}:{WEBAPP_PORT}")
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT, access_log=None)


if __name__ == "__main__":
    main()
