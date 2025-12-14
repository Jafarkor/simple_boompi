import asyncio
import logging
from config.config import bot, dp
from handlers import general, menu, text_file_audio, admin, payments, final
from middlewares.middlewares import GeneralMiddleware
from keyboards.set_menu import set_main_menu

logging.basicConfig(level=logging.INFO)

async def main():
    logging.info("Starting bot initialization")
    try:
        await set_main_menu()
        logging.info("Main menu set")

        dp.message.middleware(GeneralMiddleware())
        dp.include_router(admin.rt)
        dp.include_router(general.rt)
        dp.include_router(payments.rt)
        dp.include_router(menu.rt)
        dp.include_router(text_file_audio.rt)
        dp.include_router(final.rt)
        logging.info("Routers included")

        await bot.delete_webhook(drop_pending_updates=True)
        logging.info("Webhook deleted, starting polling")

        await dp.start_polling(bot)
        logging.info("Polling started")

    except Exception as e:
        logging.error(f"Error in main: {e}")

if __name__ == "__main__":
    asyncio.run(main())