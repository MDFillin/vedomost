"""Фоновый цикл: отправляет отметки по расписанию и закрывает просроченные."""

import asyncio
import logging

from aiogram import Bot

from .db import Database
from .service import close_session, due_slots, notify_admins, send_session, slot_key
from .utils import esc, now_local

log = logging.getLogger(__name__)

TICK_SECONDS = 20


async def tick(bot: Bot, db: Database, admin_ids: frozenset[int], failed: set[str]) -> None:
    now = now_local(db)

    for day, t in due_slots(db, now):
        key = slot_key(day, t)
        if key in failed:
            continue
        if db.group_chat_id is None:
            failed.add(key)
            await notify_admins(
                bot, admin_ids,
                f"⚠️ Пора отправлять отметку ({key}), но группа не привязана.\n"
                "Добавьте бота в чат группы и отправьте там /bind",
            )
            continue
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


async def run_scheduler(bot: Bot, db: Database, admin_ids: frozenset[int]) -> None:
    failed: set[str] = set()
    while True:
        try:
            await tick(bot, db, admin_ids, failed)
        except Exception:
            log.exception("Ошибка планировщика")
        await asyncio.sleep(TICK_SECONDS)
