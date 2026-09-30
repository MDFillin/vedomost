"""Всё, что происходит в общем чате группы."""

import logging

from aiogram import Bot, F, Router
from aiogram.filters import JOIN_TRANSITION, LEAVE_TRANSITION, ChatMemberUpdatedFilter, Command
from aiogram.types import CallbackQuery, ChatMemberUpdated, Message, User

from ..config import Config
from ..db import Database
from ..members import forget_member, is_chat_member, sync_members, sync_report
from ..service import RefreshDebouncer, is_open, notify_admins
from ..utils import esc, fmt_date, student_name

log = logging.getLogger(__name__)

GROUP_TYPES = {"group", "supergroup"}

router = Router(name="group")
router.message.filter(F.chat.type.in_(GROUP_TYPES))
router.my_chat_member.filter(F.chat.type.in_(GROUP_TYPES))
router.chat_member.filter(F.chat.type.in_(GROUP_TYPES))

debouncer = RefreshDebouncer()


def topic_of(message: Message) -> tuple[int | None, str | None]:
    """Тема форума, в которой написано сообщение: (id, название) или (None, None)."""
    if not message.is_topic_message or not message.message_thread_id:
        return None, None
    name = None
    reply = message.reply_to_message
    if reply and reply.forum_topic_created:
        name = reply.forum_topic_created.name
    return message.message_thread_id, name


async def bind_and_sync(bot: Bot, db: Database, config: Config, chat_id: int, title: str,
                        thread_id: int | None = None, topic: str | None = None) -> str:
    db.set("group_chat_id", chat_id)
    db.set("group_thread_id", thread_id or "")
    if thread_id:
        place = f"«{title}» → тема «{topic}»" if topic else f"«{title}» → тема #{thread_id}"
    else:
        place = f"«{title}»"
    db.set("group_title", place)
    if not db.get("group_name") and title:
        db.set("group_name", title)  # название для ведомости, можно поменять в настройках
    result = await sync_members(bot, db, config)
    return f"✅ Отметки будут приходить в {esc(place)}.\n\n" + sync_report(result, db, config)


# ---------- привязка ----------

@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def bot_added(event: ChatMemberUpdated, bot: Bot, db: Database, config: Config) -> None:
    """Бота добавили в чат: если это сделал староста — привязываемся и считываем всех.

    Автоматически привязываемся только к первому чату: если бот уже где-то работает,
    добавление в другой чат сообщества ничего не переключит — только /bind.
    """
    title = event.chat.title or str(event.chat.id)
    if event.from_user.id not in config.admin_ids:
        await notify_admins(
            bot, config.admin_ids,
            f"ℹ️ Меня добавил в чат «{esc(title)}» пользователь {esc(event.from_user.full_name)}.\n"
            "Если это ваша группа — отправьте в том чате /bind.",
        )
        return
    if db.group_chat_id is not None and db.group_chat_id != event.chat.id:
        await notify_admins(
            bot, config.admin_ids,
            f"ℹ️ Меня добавили в чат «{esc(title)}», но отметки по-прежнему приходят в "
            f"{esc(db.get('group_title') or 'привязанный чат')}.\n"
            "Чтобы переключить — отправьте /bind в нужном чате (или нужной теме).",
        )
        return
    report = await bind_and_sync(bot, db, config, event.chat.id, title)
    if event.chat.is_forum:
        report += (
            "\n\n🗂 Это чат с темами. Сейчас отметки будут приходить в тему «Общее». "
            "Чтобы бот писал в конкретную тему — откройте её и отправьте там /bind."
        )
    await notify_admins(bot, config.admin_ids, report)
    if not event.chat.is_forum:
        try:
            await bot.send_message(
                event.chat.id,
                "👋 Привет! Я буду собирать посещаемость: в дни занятий здесь появится "
                "сообщение с кнопкой — нажмите её, если вы на паре.",
            )
        except Exception:
            pass


@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=LEAVE_TRANSITION))
async def bot_removed(event: ChatMemberUpdated, bot: Bot, db: Database, config: Config) -> None:
    if db.group_chat_id == event.chat.id:
        await notify_admins(
            bot, config.admin_ids,
            f"⚠️ Меня удалили из чата группы «{esc(event.chat.title or '')}». "
            "Отметки отправляться не будут, пока вы не добавите меня обратно.",
        )


@router.message(Command("bind"))
async def bind_group(message: Message, bot: Bot, db: Database, config: Config) -> None:
    if not message.from_user or message.from_user.id not in config.admin_ids:
        await message.reply("Привязать бота к чату может только староста.")
        return
    thread_id, topic = topic_of(message)
    report = await bind_and_sync(
        bot, db, config, message.chat.id, message.chat.title or "", thread_id, topic
    )
    where = "в эту тему" if thread_id else "в этот чат"
    await message.reply(
        f"✅ Готово! Сообщения для отметки посещаемости будут приходить {where}. "
        "В другие чаты и темы я писать не буду."
    )
    await notify_admins(bot, config.admin_ids, report)


@router.message(Command("unbind"))
async def unbind_group(message: Message, db: Database, config: Config) -> None:
    if not message.from_user or message.from_user.id not in config.admin_ids:
        return
    if db.group_chat_id == message.chat.id:
        db.set("group_chat_id", "")
        db.set("group_thread_id", "")
        db.set("group_title", "")
        await message.reply("Чат отвязан. Отметки сюда больше приходить не будут.")


@router.message(F.migrate_to_chat_id)
async def group_migrated(message: Message, db: Database) -> None:
    # Группа превратилась в супергруппу — у неё меняется ID.
    if db.group_chat_id == message.chat.id:
        db.migrate_chat(message.chat.id, message.migrate_to_chat_id)
        log.info("Группа мигрировала: %s -> %s", message.chat.id, message.migrate_to_chat_id)


# ---------- состав чата ----------

def remember(db: Database, user: User | None) -> None:
    if user and not user.is_bot:
        db.upsert_student(user.id, user.full_name, user.username, in_chat=True)


@router.message(F.new_chat_members)
async def members_joined(message: Message, db: Database) -> None:
    if db.group_chat_id != message.chat.id:
        return
    for user in message.new_chat_members:
        remember(db, user)
        forget_member(message.chat.id, user.id)


@router.message(F.left_chat_member)
async def member_left(message: Message, db: Database) -> None:
    user = message.left_chat_member
    if db.group_chat_id != message.chat.id or user.is_bot:
        return
    db.set_in_chat(user.id, False)
    forget_member(message.chat.id, user.id)


# Приходит, только если бот — администратор чата.
@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=JOIN_TRANSITION))
async def chat_member_joined(event: ChatMemberUpdated, db: Database) -> None:
    if db.group_chat_id == event.chat.id:
        remember(db, event.new_chat_member.user)
        forget_member(event.chat.id, event.new_chat_member.user.id)


@router.chat_member(ChatMemberUpdatedFilter(member_status_changed=LEAVE_TRANSITION))
async def chat_member_left(event: ChatMemberUpdated, db: Database) -> None:
    user = event.new_chat_member.user
    if db.group_chat_id == event.chat.id and not user.is_bot:
        db.set_in_chat(user.id, False)
        forget_member(event.chat.id, user.id)


@router.message()
async def any_group_message(message: Message, db: Database) -> None:
    # Любой, кто пишет в чат группы, точно в нём состоит.
    # (Бот видит все сообщения, только если он админ или у него выключен privacy mode.)
    if db.group_chat_id == message.chat.id:
        remember(db, message.from_user)


# ---------- кнопки ----------

async def check_access(
    callback: CallbackQuery, bot: Bot, db: Database, chat_id: int, check_roster: bool = True
) -> str | None:
    """Возвращает текст отказа или None, если нажимать можно."""
    user = callback.from_user
    msg = callback.message
    # Кнопка с пересланной копии или из другого чата — не засчитываем никогда.
    if msg is None or msg.chat.id != chat_id or callback.inline_message_id:
        return "⛔ Отмечаться можно только в чате группы."
    if user.is_bot:
        return "⛔ Боты не могут отмечаться."

    student = db.get_student(user.id)
    if student and not student.active:
        return "⛔ Староста исключил вас из списка группы. Если это ошибка — напишите старосте."
    if db.get_bool("members_only") and not await is_chat_member(bot, chat_id, user.id):
        return "⛔ Отмечаться могут только участники группы."
    if check_roster and db.get_bool("roster_only") and student is None:
        return "⛔ Вас нет в списке группы. Обратитесь к старосте."
    return None


@router.callback_query(F.data.startswith("mark:"))
async def on_mark(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    session_id = int(callback.data.split(":")[1])
    session = db.get_session(session_id)
    if session is None:
        await callback.answer("Это занятие удалено.", show_alert=True)
        return

    user = callback.from_user
    if user.id in db.marks(session.id):
        await callback.answer("Вы уже отметились ✅\nПовторно нажимать не нужно.", show_alert=True)
        return

    denied = await check_access(callback, bot, db, session.chat_id)
    if denied:
        await callback.answer(denied, show_alert=True)
        return

    if not is_open(db, session):
        await callback.answer("🔒 Отметка по этому занятию уже закрыта.", show_alert=True)
        return

    remember(db, user)
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


@router.callback_query(F.data.startswith("sick:"))
async def on_sick(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    """«Я болею»: открываем личку с ботом, там человек выберет даты больничного."""
    session = db.get_session(int(callback.data.split(":")[1]))
    chat_id = session.chat_id if session else db.group_chat_id
    denied = await check_access(callback, bot, db, chat_id) if chat_id else "Группа не привязана."
    if denied:
        await callback.answer(denied, show_alert=True)
        return
    me = await bot.me()
    await callback.answer(url=f"https://t.me/{me.username}?start=sick")


@router.callback_query(F.data == "register")
async def on_register(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    chat_id = db.group_chat_id
    if chat_id is None:
        await callback.answer("Группа не привязана.", show_alert=True)
        return
    student = db.get_student(callback.from_user.id)
    if student and student.counted:
        await callback.answer("Вы уже есть в списке группы 👍", show_alert=True)
        return
    # «только из списка» здесь не проверяем — регистрация как раз для попадания в список
    denied = await check_access(callback, bot, db, chat_id, check_roster=False)
    if denied:
        await callback.answer(denied, show_alert=True)
        return
    remember(db, callback.from_user)
    await callback.answer("✅ Вы добавлены в список группы!", show_alert=True)
