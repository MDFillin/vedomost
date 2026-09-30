"""Фоновый цикл: отправляет отметки по расписанию и закрывает просроченные."""

import asyncio
import logging

from aiogram import Bot

from .config import Config
from .db import Database
from .members import sync_members
from .service import close_session, due_slots, notify_admins, send_session, slot_key, slot_sent
from .utils import esc, now_local

log = logging.getLogger(__name__)

TICK_SECONDS = 20


async def tick(bot: Bot, db: Database, config: Config, failed: set[str]) -> None:
    admin_ids = config.admin_ids
    now = now_local(db)

    for day, t in due_slots(db, now):
        key = slot_key(day, t)
        if key in failed or slot_sent(db, day, t):
            continue
        if db.group_chat_id is None:
            failed.add(key)
            await notify_admins(
                bot, admin_ids,
                f"⚠️ Пора отправлять отметку ({key}), но группа не привязана.\n"
                "Добавьте бота в чат группы и отправьте там /bind",
            )
            continue
        if config.can_read_members:
            try:  # перед отметкой сверяем состав чата: кто пришёл, кто ушёл
                await sync_members(bot, db, config)
            except Exception:
                log.exception("Не удалось обновить состав чата")
        try:
            session = await send_session(bot, db, day, t, slot=key)
            if session:
                log.info("Отправлена отметка %s", key)
        except Exception as e:
            failed.add(key)
            log.exception("Не удалось отправить отметку %s", key)
            await notify_admins(
                bot, admin_ids, f"⚠️ Не удалось отправить отметку {key} в группу:\n<code>{esc(str(e))}</code>"
            )

    for session in db.sessions_to_close(now):
        try:
            await close_session(bot, db, session, admin_ids)
        except Exception:
            log.exception("Ошибка при закрытии занятия %s", session.id)


async def run_scheduler(bot: Bot, db: Database, config: Config) -> None:
    failed: set[str] = set()
    while True:
        try:
            await tick(bot, db, config, failed)
        except Exception:
            log.exception("Ошибка планировщика")
        await asyncio.sleep(TICK_SECONDS)
