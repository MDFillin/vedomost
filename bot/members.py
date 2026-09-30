"""Состав чата группы: полное считывание участников и проверка членства."""

import asyncio
import logging
import os
import time
from dataclasses import dataclass

from aiogram import Bot

from .config import Config
from .db import Database
from .utils import esc

log = logging.getLogger(__name__)

MEMBER_STATUSES = {"creator", "administrator", "member"}
MEMBER_CACHE_SECONDS = 300

_telethon_lock = asyncio.Lock()
_member_cache: dict[tuple[int, int], tuple[bool, float]] = {}


@dataclass
class SyncResult:
    full: bool            # удалось получить полный список участников
    in_chat: int          # людей в чате по данным Telegram (без ботов)
    added: int            # новых студентов в списке
    left: int             # отмечены как вышедшие из чата
    error: str | None = None


async def fetch_all_members(config: Config, chat_id: int) -> list[tuple[int, str, str | None]]:
    """Полный список участников через MTProto (Telethon) от имени бота.

    Bot API не умеет отдавать список участников, а MTProto умеет — ботам доступно
    до ~200 участников, для учебной группы этого с запасом.
    """
    from telethon import TelegramClient

    session_path = os.path.join(os.path.dirname(os.path.abspath(config.db_path)), "telethon")
    async with _telethon_lock:
        client = TelegramClient(
            session_path, config.api_id, config.api_hash, receive_updates=False
        )
        await client.start(bot_token=config.token)
        try:
            users = await client.get_participants(chat_id)
        finally:
            await client.disconnect()

    result = []
    for u in users:
        if u.bot or u.deleted:
            continue
        name = " ".join(p for p in (u.first_name, u.last_name) if p) or str(u.id)
        result.append((u.id, name, u.username))
    return result


async def sync_members(bot: Bot, db: Database, config: Config) -> SyncResult:
    chat_id = db.group_chat_id
    if chat_id is None:
        return SyncResult(False, 0, 0, 0, "группа не привязана")

    try:
        count = await bot.get_chat_member_count(chat_id)
    except Exception:
        count = 0

    if config.can_read_members:
        try:
            members = await fetch_all_members(config, chat_id)
            added, left = db.sync_members(members)
            _member_cache.clear()
            return SyncResult(True, len(members), added, left)
        except Exception as e:
            log.exception("Не удалось считать участников через MTProto")
            error = str(e)
    else:
        error = None

    # Запасной путь через Bot API: только администраторы чата.
    added = 0
    try:
        for a in await bot.get_chat_administrators(chat_id):
            if not a.user.is_bot:
                added += db.upsert_student(a.user.id, a.user.full_name, a.user.username)
    except Exception as e:
        error = error or str(e)
    # count включает самого бота и других ботов — это оценка сверху
    return SyncResult(False, max(count - 1, 0), added, 0, error)


async def is_chat_member(bot: Bot, chat_id: int, user_id: int) -> bool:
    key = (chat_id, user_id)
    cached = _member_cache.get(key)
    if cached and time.monotonic() - cached[1] < MEMBER_CACHE_SECONDS:
        return cached[0]
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        ok = member.status in MEMBER_STATUSES or (
            member.status == "restricted" and getattr(member, "is_member", False)
        )
    except Exception as e:
        log.warning("Не удалось проверить участника %s: %s", user_id, e)
        return True  # не блокируем отметку из-за сбоя Telegram
    _member_cache[key] = (ok, time.monotonic())
    return ok


def forget_member(chat_id: int, user_id: int) -> None:
    _member_cache.pop((chat_id, user_id), None)


def sync_report(result: SyncResult, db: Database, config: Config) -> str:
    in_list = len(db.list_students())
    if result.full:
        text = (
            f"👥 Считал участников чата: <b>{result.in_chat}</b> (без ботов).\n"
            f"В списке группы: {in_list}. Новых: {result.added}, вышли из чата: {result.left}."
        )
    else:
        text = (
            f"👥 В чате примерно {result.in_chat} участников, в списке пока {in_list}.\n"
            "Telegram не даёт обычному боту полный список участников, поэтому я добавил "
            "администраторов чата и буду добавлять каждого, кто пишет в чат, заходит в него "
            "или нажимает кнопку.\n"
            "Чтобы считывать всех сразу, укажите API_ID и API_HASH в .env (см. README) "
            "или отправьте в группу «📨 Регистрация» из раздела 👥 Студенты."
        )
    if result.error and config.can_read_members:
        text += f"\n⚠️ Ошибка считывания: <code>{esc(result.error)}</code>"
    return text
