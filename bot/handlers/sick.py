"""Больничный: выбор дат в личке с ботом (открывается кнопкой «Я болею» в группе)."""

from datetime import date, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, User

from ..calendar import calendar_kb, parse_date_tokens
from ..config import Config
from ..db import Database
from ..members import is_chat_member
from ..service import notify_admins, refresh_sessions_on
from ..utils import esc, fmt_date, now_local, student_name

router = Router(name="sick")
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")

PAST_DAYS = 60     # насколько задним числом можно указать начало
FUTURE_DAYS = 90   # насколько вперёд можно указать конец
MAX_LENGTH = 90    # максимальная длина больничного в днях


class Sick(StatesGroup):
    start = State()
    end = State()
    confirm = State()


def kb(*rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows if row
    ])


def student_menu_kb() -> InlineKeyboardMarkup:
    return kb([("😷 Я болею", "sk:new")], [("📋 Мои больничные", "sk:my")])


def fmt_range(start: str, end: str) -> str:
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    if start == end:
        return f"{fmt_date(start, True)} (1 день)"
    return f"с {fmt_date(start, True)} по {fmt_date(end, True)} ({days} дн.)"


async def deny_reason(bot: Bot, db: Database, config: Config, user: User) -> str | None:
    if user.id in config.admin_ids:
        return None
    chat_id = db.group_chat_id
    if chat_id is None:
        return "Бот ещё не подключён к чату группы."
    student = db.get_student(user.id)
    if student and not student.active:
        return "Староста исключил вас из списка группы. Если это ошибка — напишите старосте."
    if db.get_bool("members_only") and not await is_chat_member(bot, chat_id, user.id):
        return "Отметить больничный могут только участники чата группы."
    if db.get_bool("roster_only") and student is None:
        return "Вас нет в списке группы. Обратитесь к старосте."
    return None


# ---------- шаги выбора дат ----------

async def show(target: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup) -> None:
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except Exception:
            await target.message.answer(text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


def whose(db: Database, data: dict) -> str:
    if not data.get("by_admin"):
        return ""
    st = db.get_student(data["target"])
    return f" — {esc(st.name) if st else data['target']}"


async def ask_start(target: Message | CallbackQuery, db: Database, state: FSMContext,
                    month: tuple[int, int] | None = None) -> None:
    await state.set_state(Sick.start)
    data = await state.get_data()
    today = now_local(db).date()
    y, m = month or (today.year, today.month)
    await state.update_data(month=f"{y}-{m:02d}")
    text = (
        f"😷 <b>Больничный{whose(db, data)}</b>\n\n"
        "Шаг 1 из 2. Выберите <b>первый день</b> болезни:\n\n"
        "<i>Или напишите даты сообщением, например <code>28.09-02.10</code> "
        "или <code>28.09</code> для одного дня.</i>"
    )
    markup = calendar_kb(
        y, m, min_day=today - timedelta(days=PAST_DAYS), max_day=today + timedelta(days=FUTURE_DAYS),
        today=today, extra=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="sk:cancel")]],
    )
    await show(target, text, markup)


async def ask_end(target: Message | CallbackQuery, db: Database, state: FSMContext,
                  month: tuple[int, int] | None = None) -> None:
    await state.set_state(Sick.end)
    data = await state.get_data()
    start = date.fromisoformat(data["start"])
    today = now_local(db).date()
    y, m = month or (start.year, start.month)
    await state.update_data(month=f"{y}-{m:02d}")
    text = (
        f"😷 <b>Больничный{whose(db, data)}</b>\n\n"
        f"Первый день: <b>{fmt_date(data['start'], True)}</b>\n\n"
        "Шаг 2 из 2. Выберите <b>последний день</b> (включительно).\n"
        "Если точно не знаете — выберите примерно, потом больничный можно удалить и указать заново."
    )
    markup = calendar_kb(
        y, m, min_day=start,
        max_day=min(start + timedelta(days=MAX_LENGTH - 1), today + timedelta(days=FUTURE_DAYS)),
        today=today, selected=start,
        extra=[
            [InlineKeyboardButton(text="1️⃣ Только этот день", callback_data=f"sk:d:{start.isoformat()}")],
            [InlineKeyboardButton(text="↩️ Другой первый день", callback_data="sk:restart"),
             InlineKeyboardButton(text="✖️ Отмена", callback_data="sk:cancel")],
        ],
    )
    await show(target, text, markup)


async def ask_confirm(target: Message | CallbackQuery, db: Database, state: FSMContext) -> None:
    await state.set_state(Sick.confirm)
    data = await state.get_data()
    await show(
        target,
        f"😷 <b>Больничный{whose(db, data)}</b>\n\n"
        f"{fmt_range(data['start'], data['end'])}\n\nВсё верно?",
        kb(
            [("✅ Сохранить", "sk:save")],
            [("✏️ Выбрать заново", "sk:restart"), ("✖️ Отмена", "sk:cancel")],
        ),
    )


async def begin(target: Message | CallbackQuery, bot: Bot, db: Database, config: Config,
                state: FSMContext, target_uid: int | None = None) -> None:
    user = target.from_user
    by_admin = target_uid is not None and user.id in config.admin_ids
    if not by_admin:
        reason = await deny_reason(bot, db, config, user)
        if reason:
            await state.clear()
            await show(target, f"⛔ {reason}", kb())
            return
    await state.clear()
    await state.update_data(target=target_uid if by_admin else user.id, by_admin=by_admin)
    await ask_start(target, db, state)


# ---------- входы ----------

@router.message(CommandStart(deep_link=True, magic=F.args == "sick"))
async def start_from_group(message: Message, command: CommandObject, bot: Bot, db: Database,
                           config: Config, state: FSMContext) -> None:
    await begin(message, bot, db, config, state)


@router.callback_query(F.data == "sk:new")
async def new_leave(callback: CallbackQuery, bot: Bot, db: Database, config: Config,
                    state: FSMContext) -> None:
    await begin(callback, bot, db, config, state)


@router.callback_query(F.data.startswith("sk:for:"))
async def new_leave_for(callback: CallbackQuery, bot: Bot, db: Database, config: Config,
                        state: FSMContext) -> None:
    if callback.from_user.id not in config.admin_ids:
        await callback.answer()
        return
    await begin(callback, bot, db, config, state, target_uid=int(callback.data.split(":")[2]))


@router.callback_query(F.data == "sk:noop")
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == "sk:cancel")
async def cancel(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    back = [("« К студенту", f"a:u:{data['target']}")] if data.get("by_admin") else []
    await show(callback, "Отменено.", kb(back) if back else student_menu_kb())


@router.callback_query(F.data == "sk:restart")
async def restart(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    if not (await state.get_data()).get("target"):
        await callback.answer("Начните заново: нажмите «😷 Я болею»", show_alert=True)
        return
    await ask_start(callback, db, state)


@router.callback_query(F.data.startswith("sk:m:"))
async def change_month(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    y, m = map(int, callback.data.split(":")[2].split("-"))
    current = await state.get_state()
    if current == Sick.start.state:
        await ask_start(callback, db, state, (y, m))
    elif current == Sick.end.state:
        await ask_end(callback, db, state, (y, m))
    else:
        await callback.answer("Начните заново: нажмите «😷 Я болею»", show_alert=True)


@router.callback_query(F.data.startswith("sk:d:"))
async def pick_day(callback: CallbackQuery, db: Database, state: FSMContext) -> None:
    day = callback.data.split(":")[2]
    current = await state.get_state()
    today = now_local(db).date()
    picked = date.fromisoformat(day)
    lo, hi = today - timedelta(days=PAST_DAYS), today + timedelta(days=FUTURE_DAYS)
    if current == Sick.end.state:
        lo = date.fromisoformat((await state.get_data())["start"])
        hi = min(hi, lo + timedelta(days=MAX_LENGTH - 1))
    if not lo <= picked <= hi:
        await callback.answer("Эту дату выбрать нельзя", show_alert=True)
        return
    if current == Sick.start.state:
        await state.update_data(start=day)
        await ask_end(callback, db, state)
    elif current == Sick.end.state:
        await state.update_data(end=day)
        await ask_confirm(callback, db, state)
    else:
        await callback.answer("Начните заново: нажмите «😷 Я болею»", show_alert=True)


@router.message(Sick.start, F.text)
@router.message(Sick.end, F.text)
async def typed_dates(message: Message, db: Database, state: FSMContext) -> None:
    today = now_local(db).date()
    dates = parse_date_tokens(message.text, today)
    data = await state.get_data()
    if not dates or len(dates) > 2:
        await message.answer(
            "Не понял даты 🤔 Напишите, например, <code>28.09-02.10</code> или <code>28.09</code>"
        )
        return
    if len(dates) == 2:
        start, end = dates
    elif await state.get_state() == Sick.end.state:
        start, end = date.fromisoformat(data["start"]), dates[0]
    else:
        start = end = dates[0]
    if end < start:
        start, end = end, start
    if start < today - timedelta(days=PAST_DAYS) or end > today + timedelta(days=FUTURE_DAYS):
        await message.answer(
            f"Можно указать даты не раньше чем за {PAST_DAYS} дней и не позже чем "
            f"через {FUTURE_DAYS} дней от сегодня."
        )
        return
    if (end - start).days + 1 > MAX_LENGTH:
        await message.answer(f"Слишком длинный больничный (больше {MAX_LENGTH} дней).")
        return
    await state.update_data(start=start.isoformat(), end=end.isoformat())
    await ask_confirm(message, db, state)


@router.callback_query(Sick.confirm, F.data == "sk:save")
async def save(callback: CallbackQuery, bot: Bot, db: Database, config: Config,
               state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    uid, by_admin = data["target"], data.get("by_admin", False)
    if not by_admin:
        db.upsert_student(uid, callback.from_user.full_name, callback.from_user.username)
    db.add_sick_leave(uid, data["start"], data["end"], by_admin=by_admin)

    start, end = date.fromisoformat(data["start"]), date.fromisoformat(data["end"])
    days = {(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)}
    await refresh_sessions_on(bot, db, days)

    period = fmt_range(data["start"], data["end"])
    if by_admin:
        await show(callback, f"✅ Больничный записан: {period}",
                   kb([("« К студенту", f"a:u:{uid}")]))
        return
    await show(
        callback,
        f"✅ Больничный записан: <b>{period}</b>\n\nСтароста увидит его в ведомости. "
        "Выздоравливайте! 🍵",
        student_menu_kb(),
    )
    if db.get_bool("notify_sick"):
        st = db.get_student(uid)
        name = student_name(st, db.get("name_format")) if st else str(uid)
        await notify_admins(
            bot, config.admin_ids - {uid}, f"😷 <b>{esc(name)}</b> на больничном: {period}"
        )


@router.callback_query(F.data == "sk:save")
async def save_without_state(callback: CallbackQuery) -> None:
    await callback.answer("Начните заново: нажмите «😷 Я болею»", show_alert=True)


# ---------- мои больничные ----------

async def show_my(target: Message | CallbackQuery, db: Database, uid: int) -> None:
    leaves = db.list_sick_leaves(uid)[:20]
    today = now_local(db).date().isoformat()
    if not leaves:
        await show(target, "📋 У вас нет больничных.", student_menu_kb())
        return
    lines = ["📋 <b>Мои больничные</b>", ""]
    rows = []
    for leave in leaves:
        mark = "🟢 " if leave.covers(today) else ""
        lines.append(f"• {mark}{fmt_range(leave.start, leave.end)}")
        if leave.end >= today and not leave.by_admin:
            rows.append([(f"❌ Удалить {fmt_date(leave.start)}", f"sk:del:{leave.id}")])
    lines.append("\nУдалить можно текущие и будущие больничные, которые вы указали сами.")
    rows.append([("😷 Новый больничный", "sk:new")])
    await show(target, "\n".join(lines), kb(*rows))


@router.callback_query(F.data == "sk:my")
async def my_leaves(callback: CallbackQuery, db: Database) -> None:
    await show_my(callback, db, callback.from_user.id)


@router.callback_query(F.data.startswith("sk:del:"))
async def delete_own(callback: CallbackQuery, bot: Bot, db: Database) -> None:
    leave = db.get_sick_leave(int(callback.data.split(":")[2]))
    today = now_local(db).date().isoformat()
    if leave and leave.user_id == callback.from_user.id and not leave.by_admin and leave.end >= today:
        db.delete_sick_leave(leave.id)
        start, end = date.fromisoformat(leave.start), date.fromisoformat(leave.end)
        await refresh_sessions_on(
            bot, db, {(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)}
        )
    await show_my(callback, db, callback.from_user.id)
