"""Сообщение с отметкой в группе, закрытие занятий и вычисление расписания."""

import asyncio
import logging
from datetime import date, datetime, time, timedelta

from aiogram import Bot
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .db import Database, Session
from .utils import esc, fmt_date, get_tz, now_local, student_name, to_local

log = logging.getLogger(__name__)

# Если бот был выключен и пропустил время отправки, он отправит сообщение
# только если опоздание не больше этого окна.
SEND_GRACE = timedelta(minutes=15)
SEARCH_DAYS_AHEAD = 60


# ---------- текст и клавиатура сообщения в группе ----------

def is_open(db: Database, session: Session) -> bool:
    if session.closed:
        return False
    if session.close_at and to_local(db, session.close_at) <= now_local(db):
        return False
    return True


def group_text(db: Database, session: Session) -> str:
    title = esc(db.get("title"))
    when = fmt_date(session.date, with_weekday=True, full_weekday=True)
    if session.time:
        when += f" · {session.time}"
    lines = [f"📋 <b>{title}</b>", f"📅 {when}", ""]

    marks = db.marks(session.id)
    opened = is_open(db, session)
    if opened:
        lines.append("Если вы на занятии — нажмите кнопку ниже 👇")
        if session.close_at:
            lines.append(f"⏳ Отметка открыта до {to_local(db, session.close_at):%H:%M}")
    else:
        lines.append("🔒 Отметка закрыта")

    if db.get_bool("show_count") or db.get_bool("show_names") or not opened:
        lines.append("")
        lines.append(f"👥 Отметились: <b>{len(marks)}</b>")

    if db.get_bool("show_names") and marks:
        fmt = db.get("name_format")
        names = []
        for uid in marks:
            st = db.get_student(uid)
            names.append(esc(student_name(st, fmt)) if st else str(uid))
        names.sort(key=str.lower)
        lines.extend(f"{i}. {n}" for i, n in enumerate(names, 1))

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    return text


def group_keyboard(db: Database, session: Session) -> InlineKeyboardMarkup | None:
    if not is_open(db, session):
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text=db.get("button_text") or "✅ Я был",
                callback_data=f"mark:{session.id}",
                style=ButtonStyle.SUCCESS,
            )
        ]]
    )


# ---------- отправка / обновление / закрытие ----------

async def notify_admins(bot: Bot, admin_ids: frozenset[int], text: str) -> None:
    for uid in admin_ids:
        try:
            await bot.send_message(uid, text)
        except Exception as e:  # админ мог не запускать бота в личке
            log.warning("Не удалось написать админу %s: %s", uid, e)


async def send_session(
    bot: Bot,
    db: Database,
    session_date: date,
    session_time: str | None,
    slot: str | None = None,
) -> Session | None:
    """Создаёт занятие и публикует сообщение с кнопкой в группе."""
    chat_id = db.group_chat_id
    if chat_id is None:
        raise RuntimeError("Группа не привязана. Отправьте /bind в нужном чате.")

    window = db.get_int("window_minutes")
    close_at = now_local(db) + timedelta(minutes=window) if window > 0 else None
    session = db.create_session(chat_id, session_date.isoformat(), session_time, slot, close_at)
    if session is None:
        return None  # этот слот уже отправлялся

    try:
        msg = await bot.send_message(
            chat_id, group_text(db, session), reply_markup=group_keyboard(db, session)
        )
    except Exception:
        db.delete_session(session.id)
        raise

    db.set_session_message(session.id, msg.message_id)
    if db.get_bool("pin_message"):
        try:
            await bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)
        except Exception as e:
            log.warning("Не удалось закрепить сообщение: %s", e)
    return db.get_session(session.id)


async def refresh_group_message(bot: Bot, db: Database, session_id: int) -> None:
    session = db.get_session(session_id)
    if not session or not session.message_id:
        return
    try:
        await bot.edit_message_text(
            text=group_text(db, session),
            chat_id=session.chat_id,
            message_id=session.message_id,
            reply_markup=group_keyboard(db, session),
        )
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        await refresh_group_message(bot, db, session_id)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            log.warning("Не удалось обновить сообщение занятия %s: %s", session_id, e)
    except TelegramForbiddenError as e:
        log.warning("Нет доступа к группе: %s", e)


class RefreshDebouncer:
    """Склеивает частые обновления сообщения, чтобы не упираться в лимиты Telegram."""

    def __init__(self, delay: float = 2.0):
        self.delay = delay
        self._pending: set[int] = set()

    def schedule(self, bot: Bot, db: Database, session_id: int) -> None:
        if session_id in self._pending:
            return
        self._pending.add(session_id)
        asyncio.create_task(self._run(bot, db, session_id))

    async def _run(self, bot: Bot, db: Database, session_id: int) -> None:
        try:
            await asyncio.sleep(self.delay)
        finally:
            self._pending.discard(session_id)
        await refresh_group_message(bot, db, session_id)


def attendance_summary(db: Database, session: Session) -> tuple[list[str], list[str]]:
    """Списки имён присутствовавших и отсутствовавших."""
    fmt = db.get("name_format")
    marks = db.marks(session.id)
    present, absent = [], []
    for st in db.list_students():
        (present if st.user_id in marks else absent).append(student_name(st, fmt))
    return present, absent


async def close_session(
    bot: Bot, db: Database, session: Session, admin_ids: frozenset[int] | None = None
) -> None:
    db.set_session_closed(session.id, True, None)
    await refresh_group_message(bot, db, session.id)
    if admin_ids and db.get_bool("notify_on_close"):
        present, absent = attendance_summary(db, session)
        total = len(present) + len(absent)
        when = fmt_date(session.date, with_weekday=True)
        if session.time:
            when += f" {session.time}"
        text = [f"🔒 Отметка за <b>{when}</b> закрыта.", f"✅ Были: {len(present)}/{total}"]
        if absent:
            text.append("❌ Не было: " + ", ".join(esc(n) for n in absent))
        await notify_admins(bot, admin_ids, "\n".join(text))


async def reopen_session(bot: Bot, db: Database, session: Session) -> None:
    window = db.get_int("window_minutes")
    close_at = now_local(db) + timedelta(minutes=window) if window > 0 else None
    db.set_session_closed(session.id, False, close_at)
    await refresh_group_message(bot, db, session.id)


# ---------- расписание ----------

def slots_for_day(db: Database, day: date) -> list[str]:
    iso = day.isoformat()
    times: set[str] = set()
    if not db.is_skip_date(iso):
        times.update(t for _, wd, t in db.list_schedule() if wd == day.weekday())
    times.update(t for _, d, t in db.list_extra_dates() if d == iso)
    return sorted(times)


def slot_key(day: date, t: str) -> str:
    return f"{day.isoformat()} {t}"


def due_slots(db: Database, now: datetime) -> list[tuple[date, str]]:
    """Слоты, время которых наступило (с учётом окна опоздания) и которые ещё не отправлены."""
    tz = get_tz(db)
    result = []
    for day in {now.date(), (now - SEND_GRACE).date()}:
        for t in slots_for_day(db, day):
            hh, mm = map(int, t.split(":"))
            at = datetime.combine(day, time(hh, mm), tz)
            if at <= now < at + SEND_GRACE:
                result.append((day, t))
    return sorted(result)


def next_slot(db: Database, now: datetime) -> datetime | None:
    tz = get_tz(db)
    for i in range(SEARCH_DAYS_AHEAD):
        day = now.date() + timedelta(days=i)
        for t in slots_for_day(db, day):
            hh, mm = map(int, t.split(":"))
            at = datetime.combine(day, time(hh, mm), tz)
            if at > now:
                return at
    return None
