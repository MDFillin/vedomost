"""Админ-панель старосты в личных сообщениях с ботом."""

import csv
import io

from aiogram import Bot, F, Router
from aiogram.enums import ButtonStyle
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..config import Config
from ..db import DEFAULT_SETTINGS, Database, Session
from ..members import sync_members, sync_report
from ..service import (
    PLACEHOLDERS,
    apply_close_settings,
    close_session,
    compute_close_at,
    day_mode,
    describe_close,
    find_day_session,
    preview_text,
    is_open,
    next_slot,
    refresh_group_message,
    reopen_session,
    send_session,
)
from ..utils import (
    NAME_FORMATS,
    WEEKDAYS_FULL,
    WEEKDAYS_SHORT,
    date_range,
    esc,
    fmt_date,
    fmt_short_date,
    is_valid_tz,
    now_local,
    parse_date,
    parse_time,
    parse_times,
    pct,
    student_name,
    to_local,
)

router = Router(name="admin")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

SESSIONS_PER_PAGE = 8
STUDENTS_PER_PAGE = 12
MARKS_PER_PAGE = 20
WINDOW_OPTIONS = [15, 30, 45, 60, 90, 120, 180, 240]


class Input(StatesGroup):
    sched_time = State()
    extra_date = State()
    skip_date = State()
    title = State()
    message_text = State()
    close_until = State()
    button = State()
    timezone = State()
    rename = State()


# ---------- helpers ----------

Row = list[tuple[str, str]]


def kb(*rows: Row) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=text, callback_data=data) for text, data in row]
            for row in rows
            if row
        ]
    )


BACK_TO_MENU: Row = [("« Главное меню", "a:menu")]
CANCEL: Row = [("✖️ Отмена", "a:cancel")]


async def show(target: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    """Редактирует текущее сообщение панели (для кнопок) или отправляет новое."""
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await target.message.answer(text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


def pager(prefix: str, page: int, total: int, per_page: int) -> Row:
    pages = max(1, (total + per_page - 1) // per_page)
    if pages == 1:
        return []
    row: Row = []
    row.append(("◀️", f"{prefix}:{page - 1}") if page > 0 else (" ", "a:noop"))
    row.append((f"{page + 1}/{pages}", "a:noop"))
    row.append(("▶️", f"{prefix}:{page + 1}") if page < pages - 1 else (" ", "a:noop"))
    return row


def session_title(session: Session) -> str:
    text = fmt_short_date(session.date)
    if session.time:
        text += f" {session.time}"
    return text


def on_off(value: bool) -> str:
    return "✅" if value else "❌"


# ---------- доступ ----------

# Для всех, кто не староста: короткое пояснение.
public_router = Router(name="public")
public_router.message.filter(F.chat.type == "private")


@public_router.message()
async def not_admin(message: Message) -> None:
    await message.answer(
        "👋 Я бот для отметки посещаемости.\n"
        "Отмечайтесь кнопкой «Я был» в общем чате группы — больше ничего делать не нужно."
    )


def setup(config: Config) -> list[Router]:
    """Роутеры лички: сначала админский (только для ADMIN_IDS), потом публичный."""
    is_admin = F.from_user.id.in_(config.admin_ids)
    router.message.filter(is_admin)
    router.callback_query.filter(is_admin)
    return [router, public_router]


@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message, db: Database, state: FSMContext) -> None:
    await state.clear()
    await show_menu(message, db)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Как пользоваться</b>\n\n"
        "1. Сами добавьте бота в общий чат группы — он привяжется и считает участников.\n"
        "2. Если бот уже был в чате — отправьте там /bind.\n"
        "3. Здесь, в личке, откройте /menu → 🗓 Расписание и добавьте дни и время.\n"
        "4. В назначенное время бот пришлёт в группу сообщение с кнопкой «✅ Я был».\n"
        "5. Статистика — в разделе 📊 Занятия, сводка по людям — в 👥 Студенты.\n\n"
        "Режим (каждая пара / раз в день), время работы кнопки, текст сообщения и защита "
        "от накрутки — в ⚙️ Настройки.\n\n"
        "Команды: /menu — панель, /cancel — отменить ввод, /bind и /unbind — в групповом чате."
    )


@router.message(Command("cancel"))
@router.callback_query(F.data == "a:cancel")
async def cancel(event: Message | CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    await show_menu(event, db)


@router.callback_query(F.data == "a:noop")
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()


# ---------- главное меню ----------

@router.callback_query(F.data == "a:menu")
async def menu_cb(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    await show_menu(callback, db)


async def show_menu(target: Message | CallbackQuery, db: Database) -> None:
    now = now_local(db)
    chat_id = db.group_chat_id
    nxt = next_slot(db, now)
    lines = ["🎓 <b>Панель старосты</b>", ""]
    if chat_id:
        lines.append(f"💬 Группа привязана (ID <code>{chat_id}</code>)")
    else:
        lines.append("⚠️ Группа не привязана — добавьте бота в чат и отправьте там /bind")
    lines.append(f"👥 Студентов в списке: {len(db.list_students())}")
    lines.append(f"📚 Занятий в журнале: {db.count_sessions()}")
    if nxt:
        lines.append(
            f"⏰ Следующая отправка: {WEEKDAYS_SHORT[nxt.weekday()]} {nxt:%d.%m %H:%M}"
        )
    else:
        lines.append("⏰ Расписание пустое")

    open_sessions = [s for s in db.list_sessions(limit=5) if is_open(db, s)]
    rows: list[Row] = []
    for s in open_sessions:
        rows.append([(f"🟢 Идёт отметка: {session_title(s)}", f"a:s:{s.id}")])
    rows += [
        [("📊 Занятия и посещаемость", "a:sl:0")],
        [("👥 Студенты и сводка", "a:stl:0")],
        [("🗓 Расписание", "a:sch"), ("⚙️ Настройки", "a:set")],
        [("📤 Отправить отметку сейчас", "a:send")],
        [("📥 Выгрузить таблицу (CSV)", "a:csv")],
    ]
    await show(target, "\n".join(lines), kb(*rows))


# ---------- занятия ----------

@router.callback_query(F.data.startswith("a:sl:"))
async def sessions_list(callback: CallbackQuery, db: Database) -> None:
    await show_sessions_list(callback, db, int(callback.data.split(":")[2]))


async def show_sessions_list(callback: CallbackQuery, db: Database, page: int) -> None:
    total = db.count_sessions()
    sessions = db.list_sessions(SESSIONS_PER_PAGE, page * SESSIONS_PER_PAGE)
    students_total = len(db.list_students())

    rows: list[Row] = []
    for s in sessions:
        present = len(db.marks(s.id))
        icon = "🟢" if is_open(db, s) else "📅"
        rows.append([(f"{icon} {session_title(s)} — {present}/{students_total}", f"a:s:{s.id}")])
    rows.append(pager("a:sl", page, total, SESSIONS_PER_PAGE))
    rows.append(BACK_TO_MENU)

    text = "📊 <b>Занятия</b>\n\nВыберите день, чтобы увидеть кто был и кого не было."
    if not total:
        text = "📊 <b>Занятия</b>\n\nПока ни одной отметки не было."
    await show(callback, text, kb(*rows))


def session_card(db: Database, session: Session) -> str:
    fmt = db.get("name_format")
    marks = db.marks(session.id)
    students = db.list_students()
    present = [s for s in students if s.user_id in marks]
    absent = [s for s in students if s.user_id not in marks]
    # отметившиеся, которых староста исключил из списка
    others = [uid for uid in marks if uid not in {s.user_id for s in students}]

    if db.get("stats_sort") == "time":
        present.sort(key=lambda s: marks[s.user_id].marked_at)

    when = fmt_date(session.date, with_weekday=True, full_weekday=True)
    if session.time:
        when += f" · {session.time}"
    status = "🟢 отметка открыта" if is_open(db, session) else "🔒 отметка закрыта"
    if is_open(db, session) and session.close_at:
        status += f" до {to_local(db, session.close_at):%H:%M}"

    total = len(students)
    lines = [
        f"📅 <b>{when}</b>",
        status,
        "",
        f"✅ <b>Были: {len(present)}/{total}</b> ({pct(len(present), total)})",
    ]
    for i, st in enumerate(present, 1):
        m = marks[st.user_id]
        mark_time = "вручную" if m.by_admin else f"{to_local(db, m.marked_at):%H:%M}"
        lines.append(f"{i}. {esc(student_name(st, fmt))} <i>— {mark_time}</i>")
    lines += ["", f"❌ <b>Не было: {len(absent)}</b>"]
    lines += [f"{i}. {esc(student_name(st, fmt))}" for i, st in enumerate(absent, 1)]
    if others:
        lines += ["", f"ℹ️ Ещё отметились (не в списке): {len(others)}"]

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n…"
    return text


async def show_session(target: Message | CallbackQuery, db: Database, session_id: int) -> None:
    session = db.get_session(session_id)
    if session is None:
        await show(target, "Занятие не найдено.", kb(BACK_TO_MENU))
        return
    toggle = (
        ("🔒 Закрыть отметку", f"a:sc:{session.id}")
        if is_open(db, session)
        else ("🔓 Открыть снова", f"a:so:{session.id}")
    )
    markup = kb(
        [("✏️ Исправить отметки", f"a:se:{session.id}:0")],
        [toggle, ("🔄 Обновить", f"a:s:{session.id}")],
        [("🗑 Удалить занятие", f"a:sd:{session.id}")],
        [("« К списку занятий", "a:sl:0")],
    )
    await show(target, session_card(db, session), markup)


@router.callback_query(F.data.startswith("a:s:"))
async def session_view(callback: CallbackQuery, db: Database) -> None:
    await show_session(callback, db, int(callback.data.split(":")[2]))


@router.callback_query(F.data.startswith("a:sc:"))
async def session_close(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    session = db.get_session(int(callback.data.split(":")[2]))
    if session:
        await close_session(bot, db, session)
    await show_session(callback, db, int(callback.data.split(":")[2]))


@router.callback_query(F.data.startswith("a:so:"))
async def session_reopen(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    session = db.get_session(int(callback.data.split(":")[2]))
    if session:
        await reopen_session(bot, db, session)
    await show_session(callback, db, int(callback.data.split(":")[2]))


@router.callback_query(F.data.startswith("a:sd:"))
async def session_delete_ask(callback: CallbackQuery, db: Database) -> None:
    session = db.get_session(int(callback.data.split(":")[2]))
    if session is None:
        await show(callback, "Занятие не найдено.", kb(BACK_TO_MENU))
        return
    await show(
        callback,
        f"Удалить занятие <b>{session_title(session)}</b> вместе со всеми отметками?\n"
        "Сообщение в группе тоже будет удалено.",
        kb(
            [("🗑 Да, удалить", f"a:sdy:{session.id}")],
            [("« Нет, назад", f"a:s:{session.id}")],
        ),
    )


@router.callback_query(F.data.startswith("a:sdy:"))
async def session_delete(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    session = db.get_session(int(callback.data.split(":")[2]))
    if session:
        if session.message_id:
            try:
                await bot.delete_message(session.chat_id, session.message_id)
            except TelegramBadRequest:
                pass
        db.delete_session(session.id)
    await show_sessions_list(callback, db, 0)


@router.callback_query(F.data.startswith("a:se:"))
async def session_edit(callback: CallbackQuery, db: Database) -> None:
    _, _, sid, page = callback.data.split(":")
    await show_session_edit(callback, db, int(sid), int(page))


async def show_session_edit(callback: CallbackQuery, db: Database, sid: int, page: int) -> None:
    session = db.get_session(sid)
    if session is None:
        await show(callback, "Занятие не найдено.", kb(BACK_TO_MENU))
        return
    fmt = db.get("name_format")
    marks = db.marks(sid)
    students = db.list_students()
    chunk = students[page * MARKS_PER_PAGE:(page + 1) * MARKS_PER_PAGE]

    rows: list[Row] = []
    pair: Row = []
    for st in chunk:
        icon = "✅" if st.user_id in marks else "⬜"
        pair.append((f"{icon} {student_name(st, fmt)}", f"a:st:{sid}:{st.user_id}:{page}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    rows.append(pair)
    rows.append(pager(f"a:se:{sid}", page, len(students), MARKS_PER_PAGE))
    rows.append([("✔️ Готово", f"a:s:{sid}")])

    text = (
        f"✏️ <b>Исправление отметок — {session_title(session)}</b>\n\n"
        "Нажмите на студента, чтобы поставить или снять отметку.\n"
        f"Сейчас отмечено: {len(marks)}/{len(students)}"
    )
    if not students:
        text += "\n\nСписок студентов пуст."
    await show(callback, text, kb(*rows))


@router.callback_query(F.data.startswith("a:st:"))
async def session_toggle_mark(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    _, _, sid, uid, page = callback.data.split(":")
    sid, uid = int(sid), int(uid)
    if uid in db.marks(sid):
        db.unmark(sid, uid)
    else:
        db.mark(sid, uid, by_admin=True)
    await refresh_group_message(bot, db, sid)
    await show_session_edit(callback, db, sid, int(page))


# ---------- студенты ----------

@router.callback_query(F.data.startswith("a:stl:"))
async def students_list(callback: CallbackQuery, db: Database) -> None:
    await show_students_list(callback, db, int(callback.data.split(":")[2]))


async def show_students_list(callback: CallbackQuery, db: Database, page: int) -> None:
    fmt = db.get("name_format")
    students = db.list_students(include_inactive=True)
    students.sort(key=lambda s: (not s.counted, s.name.lower()))
    active = [s for s in students if s.counted]
    left = len([s for s in students if s.active and not s.in_chat])
    total_sessions = db.count_sessions()
    counts = db.attendance_counts()

    lines = [
        "👥 <b>Студенты и сводка посещаемости</b>",
        f"Всего занятий: {total_sessions} · в списке: {len(active)}"
        + (f" · вышли из чата: {left}" if left else ""),
        "",
    ]
    ranked = sorted(active, key=lambda s: (-counts.get(s.user_id, 0), s.name.lower()))
    for i, st in enumerate(ranked, 1):
        n = counts.get(st.user_id, 0)
        lines.append(
            f"{i}. {esc(student_name(st, fmt))} — {n}/{total_sessions} ({pct(n, total_sessions)})"
        )
    if not active:
        lines.append(
            "Список пуст. Нажмите «🔄 Считать участников чата» — или студенты появятся "
            "после первого нажатия кнопки / регистрации."
        )
    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "\n…"

    chunk = students[page * STUDENTS_PER_PAGE:(page + 1) * STUDENTS_PER_PAGE]
    rows: list[Row] = []
    pair: Row = []
    for st in chunk:
        prefix = "" if st.counted else ("🚫 " if not st.active else "🚪 ")
        pair.append((prefix + student_name(st, fmt), f"a:u:{st.user_id}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    rows.append(pair)
    rows.append(pager("a:stl", page, len(students), STUDENTS_PER_PAGE))
    rows.append([("🔄 Считать участников чата", "a:sync")])
    rows.append([("📨 Регистрация в группе", "a:reg")])
    rows.append(BACK_TO_MENU)
    await show(callback, text, kb(*rows))


@router.callback_query(F.data == "a:sync")
async def members_sync(callback: CallbackQuery, bot: Bot, db: Database, config: Config) -> None:
    if db.group_chat_id is None:
        await callback.answer("Сначала привяжите группу командой /bind в чате", show_alert=True)
        return
    result = await sync_members(bot, db, config)
    await callback.message.answer(sync_report(result, db, config))
    await show_students_list(callback, db, 0)


async def show_student(target: Message | CallbackQuery, db: Database, uid: int) -> None:
    st = db.get_student(uid)
    if st is None:
        await show(target, "Студент не найден.", kb([("« К списку", "a:stl:0")]))
        return
    sessions = db.list_sessions()
    attended = db.student_session_ids(uid)
    missed = [s for s in sessions if s.id not in attended]
    n = len([s for s in sessions if s.id in attended])

    lines = [
        f"👤 <b>{esc(st.name)}</b>",
        f"Имя в Telegram: {esc(st.full_name)}",
        f"Username: @{esc(st.username)}" if st.username else "Username: —",
        f"ID: <code>{st.user_id}</code>",
        "Статус: " + (
            "🚫 исключён старостой" if not st.active
            else "🚪 вышел из чата" if not st.in_chat
            else "в списке"
        ),
        "",
        f"✅ Посетил: {n}/{len(sessions)} ({pct(n, len(sessions))})",
        f"❌ Пропустил: {len(missed)}",
    ]
    if missed:
        lines.append("")
        lines.append("Последние пропуски:")
        lines += [f"• {fmt_date(s.date, with_weekday=True)}" + (f" {s.time}" if s.time else "")
                  for s in missed[:15]]

    markup = kb(
        [("✏️ Переименовать", f"a:ur:{uid}"), ("↩️ Имя из Telegram", f"a:urr:{uid}")] if st.custom_name
        else [("✏️ Переименовать", f"a:ur:{uid}")],
        [("🚫 Исключить из списка", f"a:ua:{uid}") if st.active
         else ("♻️ Вернуть в список", f"a:ua:{uid}")],
        [("🗑 Удалить совсем", f"a:ud:{uid}")],
        [("« К списку", "a:stl:0")],
    )
    await show(target, "\n".join(lines), markup)


@router.callback_query(F.data.startswith("a:u:"))
async def student_view(callback: CallbackQuery, db: Database) -> None:
    await show_student(callback, db, int(callback.data.split(":")[2]))


@router.callback_query(F.data.startswith("a:ua:"))
async def student_toggle_active(callback: CallbackQuery, db: Database) -> None:
    uid = int(callback.data.split(":")[2])
    st = db.get_student(uid)
    if st:
        db.set_student_active(uid, not st.active)
    await show_student(callback, db, uid)


@router.callback_query(F.data.startswith("a:urr:"))
async def student_reset_name(callback: CallbackQuery, db: Database) -> None:
    uid = int(callback.data.split(":")[2])
    db.rename_student(uid, None)
    await show_student(callback, db, uid)


@router.callback_query(F.data.startswith("a:ur:"))
async def student_rename_ask(callback: CallbackQuery, state: FSMContext) -> None:
    uid = int(callback.data.split(":")[2])
    await state.set_state(Input.rename)
    await state.update_data(uid=uid)
    await show(
        callback,
        "✏️ Отправьте новое имя студента (например, «Иванов Иван»).",
        kb([("✖️ Отмена", f"a:u:{uid}")]),
    )


@router.message(Input.rename, F.text)
async def student_rename(message: Message, db: Database, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    name = message.text.strip()[:64]
    db.rename_student(data["uid"], name)
    await show_student(message, db, data["uid"])


@router.callback_query(F.data.startswith("a:ud:"))
async def student_delete_ask(callback: CallbackQuery, db: Database) -> None:
    uid = int(callback.data.split(":")[2])
    st = db.get_student(uid)
    name = esc(st.name) if st else str(uid)
    await show(
        callback,
        f"Удалить <b>{name}</b> и все его отметки?\n"
        "Если человек просто ушёл из группы, лучше «Исключить из списка» — история сохранится.",
        kb([("🗑 Да, удалить", f"a:udy:{uid}")], [("« Нет, назад", f"a:u:{uid}")]),
    )


@router.callback_query(F.data.startswith("a:udy:"))
async def student_delete(callback: CallbackQuery, db: Database) -> None:
    db.delete_student(int(callback.data.split(":")[2]))
    await show_students_list(callback, db, 0)


@router.callback_query(F.data == "a:reg")
async def registration_ask(callback: CallbackQuery, db: Database) -> None:
    await show(
        callback,
        "📨 Бот отправит в группу сообщение с кнопкой «🙋 Я в группе».\n"
        "Каждый, кто нажмёт, попадёт в список студентов — так в статистике будет видно "
        "и тех, кто ни разу не отмечался.",
        kb([("📨 Отправить", "a:regy")], [("« Назад", "a:stl:0")]),
    )


@router.callback_query(F.data == "a:regy")
async def registration_send(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    if db.group_chat_id is None:
        await callback.answer("Сначала привяжите группу командой /bind в чате", show_alert=True)
        return
    try:
        await bot.send_message(
            db.group_chat_id,
            "🙋 <b>Регистрация в списке группы</b>\n\n"
            "Нажмите кнопку ниже, чтобы староста видел вас в журнале посещаемости.",
            reply_markup=kb([("🙋 Я в группе", "register")]),
        )
    except Exception as e:
        await callback.answer(f"Не удалось отправить: {e}", show_alert=True)
        return
    await callback.answer("Отправлено в группу ✅", show_alert=True)


# ---------- отправка вручную ----------

@router.callback_query(F.data == "a:send")
async def send_ask(callback: CallbackQuery, db: Database) -> None:
    if db.group_chat_id is None:
        await callback.answer(
            "Группа не привязана. Добавьте бота в чат группы и отправьте там /bind",
            show_alert=True,
        )
        return
    now = now_local(db)
    if day_mode(db):
        existing = find_day_session(db, now.date())
        if existing:
            await callback.answer(
                "Режим «раз в день»: отметка за сегодня уже есть.", show_alert=True
            )
            await show_session(callback, db, existing.id)
            return
    close_at = compute_close_at(db, now)
    until = (
        f"Кнопка будет активна до {close_at:%H:%M}."
        if close_at else "Кнопка будет активна, пока вы не закроете отметку."
    )
    await show(
        callback,
        f"📤 Отправить в группу отметку за <b>{fmt_date(now.date().isoformat(), True, True)}</b>?\n"
        + until,
        kb([("📤 Отправить", "a:sendy")], BACK_TO_MENU),
    )


@router.callback_query(F.data == "a:sendy")
async def send_now(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    now = now_local(db)
    try:
        session = await send_session(bot, db, now.date(), f"{now:%H:%M}")
    except Exception as e:
        await callback.answer(f"Не удалось отправить: {e}", show_alert=True)
        return
    if session is None:
        await callback.answer("Отметка за сегодня уже отправлена.", show_alert=True)
        return
    await callback.answer("Отправлено в группу ✅")
    await show_session(callback, db, session.id)


# ---------- расписание ----------

async def show_schedule(target: Message | CallbackQuery, db: Database) -> None:
    now = now_local(db)
    db.delete_past_extra_dates(now.date().isoformat())
    regular = db.list_schedule()
    extra = db.list_extra_dates()
    skips = db.list_skip_dates()
    nxt = next_slot(db, now)

    lines = ["🗓 <b>Расписание отправки</b>", ""]
    lines.append("<b>Каждую неделю:</b>")
    if regular:
        by_day: dict[int, list[str]] = {}
        for _, wd, t in regular:
            by_day.setdefault(wd, []).append(t)
        lines += [f"• {WEEKDAYS_FULL[wd]}: {', '.join(ts)}" for wd, ts in sorted(by_day.items())]
    else:
        lines.append("— нет")
    lines += ["", "<b>Разовые даты:</b>"]
    lines += [f"• {fmt_date(d, True)} в {t}" for _, d, t in extra] or ["— нет"]
    lines += ["", "<b>Пропуски (праздники, каникулы):</b>"]
    lines += [f"• {fmt_date(d, True)}" for d in skips[:30]] or ["— нет"]
    if len(skips) > 30:
        lines.append(f"… и ещё {len(skips) - 30}")
    lines += ["", f"🌍 Часовой пояс: {esc(db.get('timezone'))}, сейчас {now:%H:%M}"]
    if nxt:
        lines.append(f"⏰ Следующая отправка: {fmt_date(nxt.date().isoformat(), True)} в {nxt:%H:%M}")

    has_items = bool(regular or extra or skips)
    await show(
        target,
        "\n".join(lines),
        kb(
            [("➕ Дни недели", "a:schw"), ("➕ Разовая дата", "a:schd")],
            [("🚫 Добавить пропуск", "a:schs")],
            [("🗑 Удалить из расписания", "a:schx")] if has_items else [],
            BACK_TO_MENU,
        ),
    )


@router.callback_query(F.data == "a:sch")
async def schedule_view(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    await show_schedule(callback, db)


def weekdays_kb(selected: set[int]) -> InlineKeyboardMarkup:
    days = [
        (("✅ " if i in selected else "") + WEEKDAYS_SHORT[i], f"a:schwt:{i}") for i in range(7)
    ]
    return kb(
        days[:4],
        days[4:],
        [("Далее ➡️", "a:schwn")] if selected else [],
        [("✖️ Отмена", "a:sch")],
    )


@router.callback_query(F.data == "a:schw")
async def schedule_weekdays(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.update_data(weekdays=[])
    await show(
        callback,
        "Выберите дни недели, в которые бот будет присылать отметку:",
        weekdays_kb(set()),
    )


@router.callback_query(F.data.startswith("a:schwt:"))
async def schedule_weekday_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    wd = int(callback.data.split(":")[2])
    selected = set((await state.get_data()).get("weekdays", []))
    selected ^= {wd}
    await state.update_data(weekdays=sorted(selected))
    await show(
        callback,
        "Выберите дни недели, в которые бот будет присылать отметку:",
        weekdays_kb(selected),
    )


@router.callback_query(F.data == "a:schwn")
async def schedule_weekdays_next(callback: CallbackQuery, state: FSMContext) -> None:
    selected = (await state.get_data()).get("weekdays", [])
    await state.set_state(Input.sched_time)
    days = ", ".join(WEEKDAYS_SHORT[i] for i in selected)
    await show(
        callback,
        f"Дни: <b>{days}</b>\n\n"
        "Отправьте время отправки, например <code>09:00</code>.\n"
        "Можно несколько через пробел для нескольких пар: <code>09:00 13:40</code>",
        kb([("✖️ Отмена", "a:sch")]),
    )


@router.message(Input.sched_time, F.text)
async def schedule_time_input(message: Message, db: Database, state: FSMContext) -> None:
    times = parse_times(message.text)
    if not times:
        await message.answer(
            "Не понял время 🤔 Формат: <code>09:00</code> или <code>09:00 13:40</code>",
            reply_markup=kb(CANCEL),
        )
        return
    weekdays = (await state.get_data()).get("weekdays", [])
    for wd in weekdays:
        for t in times:
            db.add_schedule(wd, t)
    await state.clear()
    await message.answer("✅ Расписание обновлено")
    await show_schedule(message, db)


@router.callback_query(F.data == "a:schd")
async def schedule_extra_ask(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Input.extra_date)
    await show(
        callback,
        "Отправьте дату и время разовой отметки, например:\n"
        "<code>15.10.2026 10:30</code>\n"
        "Можно несколько — каждую с новой строки. Год можно не писать: <code>15.10 10:30</code>",
        kb([("✖️ Отмена", "a:sch")]),
    )


@router.message(Input.extra_date, F.text)
async def schedule_extra_input(message: Message, db: Database, state: FSMContext) -> None:
    today = now_local(db).date()
    parsed = []
    for line in message.text.strip().splitlines():
        parts = line.split()
        if not parts:
            continue
        d = parse_date(parts[0], today)
        t = parse_time(parts[1]) if len(parts) == 2 else None
        if d is None or t is None:
            await message.answer(
                f"Не понял строку «{esc(line)}» 🤔\nФормат: <code>15.10.2026 10:30</code>",
                reply_markup=kb(CANCEL),
            )
            return
        if d < today:
            await message.answer(f"Дата {d:%d.%m.%Y} уже прошла.", reply_markup=kb(CANCEL))
            return
        parsed.append((d, t))
    if not parsed:
        return
    for d, t in parsed:
        db.add_extra_date(d.isoformat(), t)
    await state.clear()
    await message.answer(f"✅ Добавлено дат: {len(parsed)}")
    await show_schedule(message, db)


@router.callback_query(F.data == "a:schs")
async def schedule_skip_ask(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Input.skip_date)
    await show(
        callback,
        "Отправьте дату, в которую отметку <b>не присылать</b> (праздник, отмена пары):\n"
        "<code>04.11.2026</code>\n\n"
        "Или период, например каникулы:\n<code>29.12.2026-11.01.2027</code>\n"
        "Можно несколько строк.",
        kb([("✖️ Отмена", "a:sch")]),
    )


@router.message(Input.skip_date, F.text)
async def schedule_skip_input(message: Message, db: Database, state: FSMContext) -> None:
    today = now_local(db).date()
    days = []
    for line in message.text.strip().splitlines():
        line = line.strip().replace("—", "-").replace("–", "-").replace(" ", "")
        if not line:
            continue
        if "-" in line:
            a, _, b = line.partition("-")
            start, end = parse_date(a, today), parse_date(b, today)
            if start and end and end < start and not b.count(".") == 2:
                end = end.replace(year=end.year + 1)
            if start is None or end is None or abs((end - start).days) > 366:
                await message.answer(
                    f"Не понял период «{esc(line)}» 🤔", reply_markup=kb(CANCEL)
                )
                return
            days += date_range(start, end)
        else:
            d = parse_date(line, today)
            if d is None:
                await message.answer(f"Не понял дату «{esc(line)}» 🤔", reply_markup=kb(CANCEL))
                return
            days.append(d)
    if not days:
        return
    for d in days:
        db.add_skip_date(d.isoformat())
    await state.clear()
    await message.answer(f"✅ Добавлено дней-пропусков: {len(set(days))}")
    await show_schedule(message, db)


@router.callback_query(F.data == "a:schx")
async def schedule_delete_menu(callback: CallbackQuery, db: Database) -> None:
    rows: list[Row] = []
    for item_id, wd, t in db.list_schedule():
        rows.append([(f"❌ {WEEKDAYS_FULL[wd]} {t}", f"a:schxw:{item_id}")])
    for item_id, d, t in db.list_extra_dates():
        rows.append([(f"❌ {fmt_date(d, True)} {t}", f"a:schxd:{item_id}")])
    for d in db.list_skip_dates()[:40]:
        rows.append([(f"❌ пропуск {fmt_date(d, True)}", f"a:schxs:{d}")])
    if db.list_skip_dates():
        rows.append([("🧹 Удалить все пропуски", "a:schxsa")])
    rows.append([("✔️ Готово", "a:sch")])
    await show(callback, "Нажмите на пункт, чтобы удалить его:", kb(*rows))


@router.callback_query(F.data.startswith("a:schx"))
async def schedule_delete_item(callback: CallbackQuery, db: Database) -> None:
    _, kind, *rest = callback.data.split(":", 2)
    value = rest[0] if rest else ""
    if kind == "schxw":
        db.delete_schedule(int(value))
    elif kind == "schxd":
        db.delete_extra_date(int(value))
    elif kind == "schxs":
        db.delete_skip_date(value)
    elif kind == "schxsa":
        for d in db.list_skip_dates():
            db.delete_skip_date(d)
    await schedule_delete_menu(callback, db)


# ---------- настройки ----------

MODE_NAMES = {"pair": "на каждой паре", "day": "раз в день"}


def protection_summary(db: Database) -> str:
    parts = []
    if db.get_bool("members_only"):
        parts.append("только участники чата")
    if db.get_bool("roster_only"):
        parts.append("только из списка")
    if db.get_bool("protect_content"):
        parts.append("запрет пересылки")
    return ", ".join(parts) or "выключена"


async def show_settings(target: Message | CallbackQuery, db: Database) -> None:
    sort_text = "по алфавиту" if db.get("stats_sort") == "name" else "по времени отметки"
    name_text = NAME_FORMATS.get(db.get("name_format"), "Имя Фамилия")
    mode_text = MODE_NAMES.get(db.get("mode"), "на каждой паре")
    close_text = describe_close(db)

    text = (
        "⚙️ <b>Настройки</b>\n\n"
        f"🔁 Режим отметки: <b>{mode_text}</b>\n"
        f"⏳ Кнопка активна: <b>{close_text}</b>\n"
        f"🛡 Защита: {protection_summary(db)}\n\n"
        f"🏷 Формат имён: {name_text}\n"
        f"↕️ Сортировка «кто был»: {sort_text}\n"
        f"🌍 Часовой пояс: {esc(db.get('timezone'))}"
    )
    markup = kb(
        [("📝 Текст сообщения и кнопки", "a:setm")],
        [(f"🔁 Режим: {mode_text}", "a:setmode")],
        [(f"⏳ Кнопка активна: {close_text}", "a:setw")],
        [("🛡 Защита от накрутки", "a:setp")],
        [(f"{on_off(db.get_bool('show_count'))} Счётчик в группе", "a:sett:show_count"),
         (f"{on_off(db.get_bool('show_names'))} Имена в группе", "a:sett:show_names")],
        [(f"{on_off(db.get_bool('pin_message'))} Закреплять", "a:sett:pin_message"),
         (f"{on_off(db.get_bool('notify_on_close'))} Итог мне", "a:sett:notify_on_close")],
        [(f"🏷 Имена: {name_text}", "a:setn"), (f"↕️ {sort_text}", "a:sets")],
        [("🌍 Часовой пояс", "a:seti:timezone")],
        BACK_TO_MENU,
    )
    await show(target, text, markup)


@router.callback_query(F.data == "a:set")
async def settings_view(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    await show_settings(callback, db)


TOGGLES = {
    "show_count", "show_names", "pin_message", "notify_on_close",
    "members_only", "roster_only", "protect_content",
}


@router.callback_query(F.data.startswith("a:sett:"))
async def settings_toggle(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    key = callback.data.split(":")[2]
    if key in TOGGLES:
        db.toggle(key)
    if key in {"show_count", "show_names"}:
        await refresh_open_sessions(bot, db)
    if key in {"members_only", "roster_only", "protect_content"}:
        await show_protection(callback, db)
    else:
        await show_settings(callback, db)


async def refresh_open_sessions(bot: Bot, db: Database) -> None:
    for s in db.list_sessions(limit=10):
        if is_open(db, s):
            await refresh_group_message(bot, db, s.id)


# --- текст сообщения ---

async def show_message_settings(target: Message | CallbackQuery, db: Database) -> None:
    placeholders = "\n".join(f"<code>{k}</code> — {v}" for k, v in PLACEHOLDERS.items())
    text = (
        "📝 <b>Сообщение в группе</b>\n"
        "Так оно будет выглядеть:\n"
        "┈┈┈┈┈┈┈┈┈┈┈┈\n"
        f"{preview_text(db)}\n"
        f"[ {esc(db.get('button_text'))} ]\n"
        "┈┈┈┈┈┈┈┈┈┈┈┈\n\n"
        "В тексте можно использовать подстановки:\n"
        f"{placeholders}\n\n"
        "Жирный, курсив и другое форматирование Telegram сохраняются."
    )
    await show(
        target,
        text,
        kb(
            [("✏️ Заголовок", "a:seti:title"), ("✏️ Текст", "a:seti:message_text")],
            [("🔘 Текст кнопки", "a:seti:button")],
            [("👁 Прислать предпросмотр", "a:setpv")],
            [("↩️ Вернуть стандартный текст", "a:setmr")],
            [("« Назад", "a:set")],
        ),
    )


@router.callback_query(F.data == "a:setm")
async def settings_message(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.clear()
    await show_message_settings(callback, db)


@router.callback_query(F.data == "a:setpv")
async def settings_preview(callback: CallbackQuery, db: Database) -> None:
    await callback.message.answer(
        preview_text(db),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text=db.get("button_text"), callback_data="a:noop", style=ButtonStyle.SUCCESS,
        )]]),
    )
    await callback.answer("Предпросмотр ниже 👇")


@router.callback_query(F.data == "a:setmr")
async def settings_message_reset(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    for key in ("title", "message_text", "button_text"):
        db.set(key, DEFAULT_SETTINGS[key])
    await refresh_open_sessions(bot, db)
    await show_message_settings(callback, db)


# --- режим ---

@router.callback_query(F.data == "a:setmode")
async def settings_mode(callback: CallbackQuery, db: Database) -> None:
    current = db.get("mode")

    def label(key: str, text: str) -> tuple[str, str]:
        return (("• " if key == current else "") + text, f"a:setmode:{key}")

    await show(
        callback,
        "🔁 <b>Режим отметки</b>\n\n"
        "<b>На каждой паре</b> — сообщение приходит в каждое время из расписания. "
        "Например, при 09:00 и 13:40 будет две отметки в день, статистика — по парам.\n\n"
        "<b>Раз в день</b> — одно сообщение в день (в первое время из расписания), "
        "статистика — по дням. Подходит, если нужно просто отметить, кто пришёл на учёбу.",
        kb(
            [label("pair", "📚 На каждой паре")],
            [label("day", "📅 Раз в день")],
            [("« Назад", "a:set")],
        ),
    )


@router.callback_query(F.data.startswith("a:setmode:"))
async def settings_mode_set(callback: CallbackQuery, db: Database) -> None:
    mode = callback.data.split(":")[2]
    if mode in MODE_NAMES:
        db.set("mode", mode)
    await show_settings(callback, db)


# --- время активности кнопки ---

@router.callback_query(F.data == "a:setw")
async def settings_window(callback: CallbackQuery, db: Database) -> None:
    mode = db.get("close_mode")
    current = db.get_int("window_minutes")
    buttons = []
    for m in WINDOW_OPTIONS:
        label = f"{m // 60} ч" if m % 60 == 0 else f"{m} мин"
        if mode == "minutes" and m == current:
            label = "• " + label
        buttons.append((label, f"a:setwv:{m}"))
    rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]

    def mark(key: str, text: str) -> str:
        return ("• " if mode == key else "") + text

    until = db.get("close_until")
    await show(
        callback,
        "⏳ <b>До какого момента можно нажимать «Я был»?</b>\n\n"
        f"Сейчас: <b>{describe_close(db)}</b>\n\n"
        "• <b>N минут / часов</b> — считается от момента отправки сообщения.\n"
        "• <b>До определённого времени</b> — например, до 10:30 в тот же день.\n"
        "• <b>До конца дня</b> — до 23:59.\n\n"
        "После этого кнопка исчезнет у всех. Изменение сразу применяется и к открытой "
        "сейчас отметке.",
        kb(
            *rows,
            [(mark("until", f"🕐 До определённого времени ({until})"), "a:setwu")],
            [(mark("eod", "🌙 До конца дня"), "a:setwm:eod"),
             (mark("none", "∞ Пока не закрою"), "a:setwm:none")],
            [("« Назад", "a:set")],
        ),
    )


async def after_close_change(target: Message | CallbackQuery, bot: Bot, db: Database) -> None:
    changed = await apply_close_settings(bot, db)
    if changed and isinstance(target, Message):
        await target.answer(f"Обновил время закрытия у открытых отметок: {changed}")
    await show_settings(target, db)


@router.callback_query(F.data.startswith("a:setwv:"))
async def settings_window_set(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    db.set("window_minutes", int(callback.data.split(":")[2]))
    db.set("close_mode", "minutes")
    await after_close_change(callback, bot, db)


@router.callback_query(F.data.startswith("a:setwm:"))
async def settings_window_mode(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    mode = callback.data.split(":")[2]
    if mode in {"eod", "none"}:
        db.set("close_mode", mode)
    await after_close_change(callback, bot, db)


@router.callback_query(F.data == "a:setwu")
async def settings_until_ask(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Input.close_until)
    await show(
        callback,
        "🕐 До какого времени можно отмечаться? Отправьте время, например <code>10:30</code>.\n\n"
        "Если отметка отправлена позже этого времени, кнопка будет активна до конца дня.",
        kb([("✖️ Отмена", "a:setw")]),
    )


@router.message(Input.close_until, F.text)
async def settings_until(message: Message, bot: Bot, db: Database, state: FSMContext) -> None:
    t = parse_time(message.text)
    if t is None:
        await message.answer(
            "Не понял время 🤔 Пример: <code>10:30</code>", reply_markup=kb([("✖️ Отмена", "a:setw")])
        )
        return
    db.set("close_until", t)
    db.set("close_mode", "until")
    await state.clear()
    await after_close_change(message, bot, db)


# --- защита ---

async def show_protection(target: Message | CallbackQuery, db: Database) -> None:
    text = (
        "🛡 <b>Защита от накрутки</b>\n\n"
        "<b>Работает всегда:</b>\n"
        "• отметиться можно только один раз;\n"
        "• кнопка работает только в самом чате группы — нажатия с пересланных копий "
        "не засчитываются;\n"
        "• исключённые старостой и боты отметиться не могут.\n\n"
        "<b>Настраивается:</b>\n"
        "• <b>Только участники чата</b> — бот спрашивает у Telegram, состоит ли нажавший "
        "в группе. Посторонний, получивший кнопку, отметиться не сможет.\n"
        "• <b>Запрет пересылки</b> — сообщение с кнопкой нельзя переслать, скопировать "
        "или сохранить.\n"
        "• <b>Только из списка</b> — отмечаться могут лишь студенты из списка группы; "
        "новички не добавятся сами. Включайте, когда список уже полный."
    )
    await show(
        target,
        text,
        kb(
            [(f"{on_off(db.get_bool('members_only'))} Только участники чата",
              "a:sett:members_only")],
            [(f"{on_off(db.get_bool('protect_content'))} Запрет пересылки",
              "a:sett:protect_content")],
            [(f"{on_off(db.get_bool('roster_only'))} Только из списка", "a:sett:roster_only")],
            [("« Назад", "a:set")],
        ),
    )


@router.callback_query(F.data == "a:setp")
async def settings_protection(callback: CallbackQuery, db: Database) -> None:
    await show_protection(callback, db)


# --- прочее ---

@router.callback_query(F.data == "a:setn")
async def settings_name_format(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    order = list(NAME_FORMATS)
    current = db.get("name_format")
    nxt = order[(order.index(current) + 1) % len(order)] if current in order else order[0]
    db.set("name_format", nxt)
    if db.get_bool("show_names"):
        await refresh_open_sessions(bot, db)
    await show_settings(callback, db)


@router.callback_query(F.data == "a:sets")
async def settings_sort(callback: CallbackQuery, db: Database) -> None:
    db.set("stats_sort", "time" if db.get("stats_sort") == "name" else "name")
    await show_settings(callback, db)


INPUT_PROMPTS = {
    "title": (Input.title, "a:setm",
              "Отправьте новый заголовок сообщения (можно с форматированием).\n"
              "Например: <code>Посещаемость ИВТ-21</code>\n\n"
              "Чтобы убрать заголовок, отправьте <code>-</code>"),
    "message_text": (Input.message_text, "a:setm",
                     "Отправьте новый текст сообщения. Можно в несколько строк и с "
                     "форматированием. Подстановки: <code>{дата}</code>, <code>{день}</code>, "
                     "<code>{время}</code>, <code>{до}</code>.\n\nНапример:\n"
                     "<code>📅 {день}, {дата}\nОтметьтесь до {до}, если вы на паре 👇</code>"),
    "button": (Input.button, "a:setm",
               "Отправьте новый текст кнопки (до 40 символов).\n"
               "Например: <code>✅ Я на паре</code>"),
    "timezone": (Input.timezone, "a:set",
                 "Отправьте часовой пояс в формате IANA, например:\n"
                 "<code>Europe/Moscow</code>, <code>Asia/Yekaterinburg</code>, "
                 "<code>Asia/Novosibirsk</code>, <code>Europe/Kaliningrad</code>, "
                 "<code>Asia/Vladivostok</code>, <code>Europe/Minsk</code>, "
                 "<code>Asia/Almaty</code>"),
}


@router.callback_query(F.data.startswith("a:seti:"))
async def settings_input_ask(callback: CallbackQuery, state: FSMContext) -> None:
    st, back, prompt = INPUT_PROMPTS[callback.data.split(":")[2]]
    await state.set_state(st)
    await show(callback, prompt, kb([("✖️ Отмена", back)]))


@router.message(Input.title, F.text)
async def settings_title(message: Message, bot: Bot, db: Database, state: FSMContext) -> None:
    title = "" if message.text.strip() == "-" else message.html_text.strip()[:300]
    db.set("title", title)
    await state.clear()
    await refresh_open_sessions(bot, db)
    await show_message_settings(message, db)


@router.message(Input.message_text, F.text)
async def settings_message_text(message: Message, bot: Bot, db: Database, state: FSMContext) -> None:
    db.set("message_text", message.html_text.strip()[:3000])
    await state.clear()
    await refresh_open_sessions(bot, db)
    await show_message_settings(message, db)


@router.message(Input.button, F.text)
async def settings_button(message: Message, bot: Bot, db: Database, state: FSMContext) -> None:
    db.set("button_text", message.text.strip()[:40])
    await state.clear()
    await refresh_open_sessions(bot, db)
    await show_message_settings(message, db)


@router.message(Input.timezone, F.text)
async def settings_timezone(message: Message, db: Database, state: FSMContext) -> None:
    tz = message.text.strip()
    if not is_valid_tz(tz):
        await message.answer(
            "Такого часового пояса нет 🤔 Пример: <code>Europe/Moscow</code>",
            reply_markup=kb([("✖️ Отмена", "a:set")]),
        )
        return
    db.set("timezone", tz)
    await state.clear()
    await show_settings(message, db)


# ---------- выгрузка ----------

@router.callback_query(F.data == "a:csv")
async def export_csv(callback: CallbackQuery, db: Database) -> None:
    sessions = list(reversed(db.list_sessions()))
    students = db.list_students()
    fmt = db.get("name_format")
    marks = {s.id: db.marks(s.id) for s in sessions}

    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";")
    writer.writerow(
        ["Студент"]
        + [fmt_date(s.date) + (f" {s.time}" if s.time else "") for s in sessions]
        + ["Посетил", "Пропустил", "%"]
    )
    for st in students:
        row = [student_name(st, fmt)]
        n = 0
        for s in sessions:
            present = st.user_id in marks[s.id]
            n += present
            row.append("+" if present else "н")
        row += [n, len(sessions) - n, pct(n, len(sessions))]
        writer.writerow(row)
    writer.writerow(
        ["Итого присутствовало"]
        + [sum(1 for st in students if st.user_id in marks[s.id]) for s in sessions]
    )

    data = ("﻿" + buf.getvalue()).encode("utf-8")  # BOM, чтобы Excel понял кириллицу
    name = f"poseshaemost_{now_local(db):%Y-%m-%d}.csv"
    await callback.message.answer_document(
        BufferedInputFile(data, filename=name),
        caption="📥 Таблица посещаемости («+» — был, «н» — не был). Открывается в Excel.",
    )
    await callback.answer()


# ---------- ввод не того типа ----------

@router.message(F.text)
async def fallback(message: Message, db: Database) -> None:
    await show_menu(message, db)


