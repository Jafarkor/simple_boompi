import asyncio
import logging
from aiohttp import web
from config.config import bot, dp
from handlers import general, text_file_audio, final
from middlewares.middlewares import GeneralMiddleware
from keyboards.set_menu import set_main_menu

logging.basicConfig(level=logging.INFO)

# Конфигурация webhook
WEBHOOK_HOST = "https://boompiai.ru"
WEBHOOK_PATH = "/webhook"
WEBHOOK_URL = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

# Порт для веб-сервера
WEBAPP_HOST = "0.0.0.0"
WEBAPP_PORT = 8080

async def on_startup(app):
    """Действия при запуске бота"""
    logging.info("Starting bot initialization")
    try:
        await set_main_menu()
        logging.info("Main menu set")

        # Даем время nginx и сертификатам подняться
        await asyncio.sleep(5)

        # Сначала проверяем текущий webhook
        webhook_info = await bot.get_webhook_info()
        logging.info(f"Current webhook: {webhook_info.url}")

        if webhook_info.url != WEBHOOK_URL:
            # Удаляем старый webhook
            await bot.delete_webhook(drop_pending_updates=True)
            logging.info("Old webhook deleted")

            await asyncio.sleep(2)

            # Устанавливаем новый webhook
            result = await bot.set_webhook(
                url=WEBHOOK_URL,
                drop_pending_updates=True,
                allowed_updates=dp.resolve_used_update_types()
            )

            if result:
                logging.info(f"✅ Webhook successfully set to {WEBHOOK_URL}")
                # Проверяем установку
                webhook_info = await bot.get_webhook_info()
                logging.info(f"Webhook verification: {webhook_info}")
            else:
                logging.error("❌ Failed to set webhook")
        else:
            logging.info(f"Webhook already set to {WEBHOOK_URL}")

    except Exception as e:
        logging.error(f"Error in startup: {e}", exc_info=True)
        # Не падаем, пытаемся работать

async def on_shutdown(app):
    """Действия при остановке бота"""
    logging.info("Shutting down bot")
    await bot.session.close()

def main():
    # Регистрируем middleware и роутеры
    dp.message.middleware(GeneralMiddleware())
    dp.include_router(general.rt)
    dp.include_router(text_file_audio.rt)
    dp.include_router(final.rt)
    logging.info("Routers included")

    # Создаем aiohttp приложение
    app = web.Application()

    # Регистрируем startup и shutdown
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    # Регистрируем webhook handler от aiogram
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler
    webhook_handler = SimpleRequestHandler(dispatcher=dp, bot=bot)
    webhook_handler.register(app, path=WEBHOOK_PATH)

    # Health check endpoint
    async def health_check(request):
        return web.Response(text="OK")

    app.router.add_get("/health", health_check)

    # Запускаем веб-сервер
    logging.info(f"Starting webhook server on {WEBAPP_HOST}:{WEBAPP_PORT}")
    web.run_app(app, host=WEBAPP_HOST, port=WEBAPP_PORT)

if __name__ == "__main__":
    main()