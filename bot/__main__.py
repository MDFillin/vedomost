import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeChat

from .config import load_config
from .db import Database
from .handlers import admin, group
from .scheduler import run_scheduler


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    config = load_config()
    db = Database(config.db_path, config.default_tz)

    bot = Bot(config.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage(), db=db, config=config)
    dp.include_router(group.router)
    for r in admin.setup(config):
        dp.include_router(r)

    await bot.set_my_commands(
        [BotCommand(command="bind", description="Привязать этот чат (для старосты)")],
        scope=BotCommandScopeAllGroupChats(),
    )
    for admin_id in config.admin_ids:
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command="menu", description="Панель старосты"),
                    BotCommand(command="help", description="Как пользоваться"),
                    BotCommand(command="cancel", description="Отменить ввод"),
                ],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception:
            logging.warning("Староста %s ещё не писал боту — напишите ему /start", admin_id)

    scheduler = asyncio.create_task(run_scheduler(bot, db, config.admin_ids))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        scheduler.cancel()


if __name__ == "__main__":
    asyncio.run(main())
