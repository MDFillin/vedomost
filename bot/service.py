"""Сообщение с отметкой в группе, закрытие занятий и вычисление расписания."""

import asyncio
import logging
from datetime import date, datetime, time, timedelta

from aiogram import Bot
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .db import Database, Session
from .utils import WEEKDAYS_FULL, esc, fmt_date, get_tz, now_local, student_name, to_local

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


PLACEHOLDERS = {
    "{дата}": "дата, например 30.09.2026",
    "{день}": "день недели, например Среда",
    "{время}": "время пары из расписания",
    "{до}": "до скольки открыта отметка",
}


def render_template(db: Database, template: str, session_date: str, session_time: str | None,
                    close_at: str | None) -> str:
    d = date.fromisoformat(session_date)
    values = {
        "{дата}": d.strftime("%d.%m.%Y"),
        "{день}": WEEKDAYS_FULL[d.weekday()],
        "{время}": session_time or "",
        "{до}": f"{to_local(db, close_at):%H:%M}" if close_at else "конца занятия",
    }
    for key, value in values.items():
        template = template.replace(key, value)
    # в режиме «раз в день» {время} пустое — убираем хвостовые пробелы
    return "\n".join(line.rstrip() for line in template.strip().splitlines())


def group_text(db: Database, session: Session) -> str:
    # заголовок и текст хранятся в HTML — староста может использовать форматирование
    title = db.get("title").strip()
    body = render_template(
        db, db.get("message_text"), session.date, session.time, session.close_at
    )
    lines = []
    if title:
        lines += [f"📋 <b>{title}</b>" if "<" not in title else f"📋 {title}"]
    if body:
        lines += [body]

    marks = db.marks(session.id)
    opened = is_open(db, session)
    status = []
    if opened:
        if session.close_at:
            status.append(f"⏳ Отметка открыта до {to_local(db, session.close_at):%H:%M}")
    else:
        status.append("🔒 Отметка закрыта")
    if db.get_bool("show_count") or db.get_bool("show_names") or not opened:
        status.append(f"👥 Отметились: <b>{len(marks)}</b>")
        sick = len(db.sick_on(session.date) - set(marks)) if session.id else 0
        if sick and db.get_bool("sick_button"):
            status.append(f"😷 На больничном: <b>{sick}</b>")
    if status:
        lines.append("\n".join(status))

    if db.get_bool("show_names") and marks:
        fmt = db.get("name_format")
        names = []
        for uid in marks:
            st = db.get_student(uid)
            names.append(esc(student_name(st, fmt)) if st else str(uid))
        names.sort(key=str.lower)
        lines.append("\n".join(f"{i}. {n}" for i, n in enumerate(names, 1)))

    text = "\n\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    return text


def preview_text(db: Database) -> str:
    """Текст сообщения для предпросмотра, как если бы отметку отправили сейчас."""
    now = now_local(db)
    close_at = compute_close_at(db, now)
    fake = Session(
        id=0, chat_id=0, message_id=None, date=now.date().isoformat(),
        time=None if day_mode(db) else f"{now:%H:%M}", slot=None,
        created_at=now.isoformat(), close_at=close_at.isoformat() if close_at else None,
        closed=False,
    )
    return group_text(db, fake)


def group_keyboard(db: Database, session: Session) -> InlineKeyboardMarkup | None:
    if not is_open(db, session):
        return None
    rows = [[
        InlineKeyboardButton(
            text=db.get("button_text") or "✅ Я был",
            callback_data=f"mark:{session.id}",
            style=ButtonStyle.SUCCESS,
        )
    ]]
    if db.get_bool("sick_button"):
        rows.append([
            InlineKeyboardButton(
                text=db.get("sick_button_text") or "😷 Я болею",
                callback_data=f"sick:{session.id}",
                style=ButtonStyle.PRIMARY,
            )
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------- отправка / обновление / закрытие ----------

async def notify_admins(bot: Bot, admin_ids: frozenset[int], text: str) -> None:
    for uid in admin_ids:
        try:
            await bot.send_message(uid, text)
        except Exception as e:  # админ мог не запускать бота в личке
            log.warning("Не удалось написать админу %s: %s", uid, e)


def compute_close_at(db: Database, start: datetime) -> datetime | None:
    """Когда закрыть отметку, начатую в момент start, по текущим настройкам."""
    tz = get_tz(db)
    start = start.astimezone(tz)
    end_of_day = datetime.combine(start.date(), time(23, 59), tz)
    mode = db.get("close_mode")
    if mode == "none":
        return None
    if mode == "eod":
        return end_of_day
    if mode == "until":
        hh, mm = map(int, (db.get("close_until") or "23:59").split(":"))
        at = datetime.combine(start.date(), time(hh, mm), tz)
        return at if at > start else end_of_day
    window = db.get_int("window_minutes")
    return start + timedelta(minutes=window) if window > 0 else None


def describe_close(db: Database) -> str:
    mode = db.get("close_mode")
    if mode == "none":
        return "без ограничения"
    if mode == "eod":
        return "до конца дня"
    if mode == "until":
        return f"до {db.get('close_until')}"
    window = db.get_int("window_minutes")
    if window <= 0:
        return "без ограничения"
    if window % 60 == 0:
        return f"{window // 60} ч после отправки"
    return f"{window} мин после отправки"


def day_mode(db: Database) -> bool:
    return db.get("mode") == "day"


def find_day_session(db: Database, session_date: date) -> Session | None:
    for s in db.list_sessions(limit=50):
        if s.date == session_date.isoformat():
            return s
    return None


async def send_session(
    bot: Bot,
    db: Database,
    session_date: date,
    session_time: str | None,
    slot: str | None = None,
) -> Session | None:
    """Создаёт занятие и публикует сообщение с кнопкой в группе.

    Возвращает None, если этот слот уже отправлялся (или, в режиме «раз в день»,
    отметка за этот день уже есть).
    """
    chat_id = db.group_chat_id
    if chat_id is None:
        raise RuntimeError("Группа не привязана. Отправьте /bind в нужном чате.")

    if day_mode(db):
        if find_day_session(db, session_date):
            return None
        slot = f"day {session_date.isoformat()}"
        session_time = None

    close_at = compute_close_at(db, now_local(db))
    session = db.create_session(chat_id, session_date.isoformat(), session_time, slot, close_at)
    if session is None:
        return None  # этот слот уже отправлялся

    try:
        msg = await bot.send_message(
            chat_id,
            group_text(db, session),
            reply_markup=group_keyboard(db, session),
            protect_content=db.get_bool("protect_content"),
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


def attendance_summary(
    db: Database, session: Session
) -> tuple[list[str], list[str], list[str]]:
    """Имена присутствовавших, болеющих и отсутствовавших без причины."""
    fmt = db.get("name_format")
    marks = db.marks(session.id)
    sick = db.sick_on(session.date)
    present, ill, absent = [], [], []
    for st in db.list_students():
        name = student_name(st, fmt)
        if st.user_id in marks:
            present.append(name)
        elif st.user_id in sick:
            ill.append(name)
        else:
            absent.append(name)
    return present, ill, absent


async def refresh_sessions_on(bot: Bot, db: Database, days: set[str]) -> None:
    """Обновляет сообщения открытых отметок за указанные дни (например, после больничного)."""
    for s in db.list_sessions(limit=20):
        if s.date in days and is_open(db, s):
            await refresh_group_message(bot, db, s.id)


async def close_session(
    bot: Bot, db: Database, session: Session, admin_ids: frozenset[int] | None = None
) -> None:
    db.set_session_closed(session.id, True, None)
    await refresh_group_message(bot, db, session.id)
    if admin_ids and db.get_bool("notify_on_close"):
        present, ill, absent = attendance_summary(db, session)
        total = len(present) + len(ill) + len(absent)
        when = fmt_date(session.date, with_weekday=True)
        if session.time:
            when += f" {session.time}"
        text = [f"🔒 Отметка за <b>{when}</b> закрыта.", f"✅ Были: {len(present)}/{total}"]
        if ill:
            text.append("😷 Болеют: " + ", ".join(esc(n) for n in ill))
        if absent:
            text.append("❌ Не было: " + ", ".join(esc(n) for n in absent))
        await notify_admins(bot, admin_ids, "\n".join(text))


async def reopen_session(bot: Bot, db: Database, session: Session) -> None:
    db.set_session_closed(session.id, False, compute_close_at(db, now_local(db)))
    await refresh_group_message(bot, db, session.id)


async def apply_close_settings(bot: Bot, db: Database) -> int:
    """Пересчитывает время закрытия у открытых отметок после смены настроек."""
    changed = 0
    for s in db.list_sessions(limit=20):
        if s.closed:
            continue
        close_at = compute_close_at(db, to_local(db, s.created_at))
        db.set_session_closed(s.id, False, close_at)
        await refresh_group_message(bot, db, s.id)
        changed += 1
    return changed


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


def slot_sent(db: Database, day: date, t: str) -> bool:
    if day_mode(db):
        return find_day_session(db, day) is not None
    return db.slot_exists(slot_key(day, t))


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
