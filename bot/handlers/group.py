"""Всё, что происходит в общем чате группы."""

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from ..config import Config
from ..db import Database
from ..service import RefreshDebouncer, is_open
from ..utils import fmt_date, student_name

log = logging.getLogger(__name__)

router = Router(name="group")
router.message.filter(F.chat.type.in_({"group", "supergroup"}))

debouncer = RefreshDebouncer()


@router.message(Command("bind"))
async def bind_group(message: Message, db: Database, config: Config) -> None:
    if not message.from_user or message.from_user.id not in config.admin_ids:
        await message.reply("Привязать бота к чату может только староста.")
        return
    db.set("group_chat_id", message.chat.id)
    await message.reply(
        "✅ Этот чат привязан. Сюда будут приходить сообщения для отметки посещаемости.\n"
        "Настройки и статистика — в личных сообщениях с ботом."
    )


@router.message(Command("unbind"))
async def unbind_group(message: Message, db: Database, config: Config) -> None:
    if not message.from_user or message.from_user.id not in config.admin_ids:
        return
    if db.group_chat_id == message.chat.id:
        db.set("group_chat_id", "")
        await message.reply("Чат отвязан. Отметки сюда больше приходить не будут.")


@router.message(F.migrate_to_chat_id)
async def group_migrated(message: Message, db: Database) -> None:
    # Группа превратилась в супергруппу — у неё меняется ID.
    if db.group_chat_id == message.chat.id:
        db.set("group_chat_id", message.migrate_to_chat_id)
        log.info("Группа мигрировала: %s -> %s", message.chat.id, message.migrate_to_chat_id)


@router.callback_query(F.data.startswith("mark:"))
async def on_mark(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    session_id = int(callback.data.split(":")[1])
    session = db.get_session(session_id)
    if session is None:
        await callback.answer("Это занятие удалено.", show_alert=True)
        return

    user = callback.from_user
    db.upsert_student(user.id, user.full_name, user.username)

    if user.id in db.marks(session.id):
        await callback.answer(
            "Вы уже отметились ✅\nПовторно нажимать не нужно.", show_alert=True
        )
        return

    if not is_open(db, session):
        await callback.answer("🔒 Отметка по этому занятию уже закрыта.", show_alert=True)
        return

    if db.mark(session.id, user.id):
        student = db.get_student(user.id)
        name = student_name(student, "full") if student else user.full_name
        await callback.answer(
            f"✅ Присутствие записано!\n{name}, {fmt_date(session.date)}", show_alert=True
        )
        if db.get_bool("show_count") or db.get_bool("show_names"):
            debouncer.schedule(bot, db, session.id)
    else:
        await callback.answer("Вы уже отметились ✅\nПовторно нажимать не нужно.", show_alert=True)


@router.callback_query(F.data == "register")
async def on_register(callback: CallbackQuery, db: Database) -> None:
    user = callback.from_user
    # Исключённых старостой студентов обратно не возвращаем — это решает староста.
    if db.upsert_student(user.id, user.full_name, user.username):
        await callback.answer("✅ Вы добавлены в список группы!", show_alert=True)
    else:
        await callback.answer("Вы уже есть в списке группы 👍", show_alert=True)
