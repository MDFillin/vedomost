"""Тесты без сети: Telegram API подменяется фейковой сессией."""

import asyncio
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    SendDocument,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from bot.config import Config
from bot.db import Database
from bot.handlers import admin, group
from bot.service import due_slots, next_slot, send_session
from bot.utils import get_tz, parse_date, parse_times

ADMIN = 100
GROUP = -1001
STUDENTS = [(201, "Иван Иванов"), (202, "Пётр Петров"), (203, "Анна Смирнова")]


class FakeSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod] = []
        self.next_id = 1000

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: Any = None) -> Any:
        self.calls.append(method)
        if isinstance(method, (SendMessage, SendDocument)):
            self.next_id += 1
            return Message(
                message_id=self.next_id,
                date=datetime.now(),
                chat=Chat(id=method.chat_id, type="private" if method.chat_id > 0 else "supergroup"),
                text=getattr(method, "text", None),
            )
        if isinstance(method, EditMessageText):
            return True
        return True

    async def close(self) -> None:
        pass

    async def stream_content(self, *a: Any, **kw: Any):  # pragma: no cover
        yield b""

    def of(self, kind: type) -> list:
        return [c for c in self.calls if isinstance(c, kind)]


@pytest.fixture
def env():
    db = Database(":memory:", "Europe/Moscow")
    config = Config(token="1:x", admin_ids=frozenset({ADMIN}), db_path=":memory:",
                    default_tz="Europe/Moscow")
    session = FakeSession()
    bot = Bot("42:TEST", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher(storage=MemoryStorage(), db=db, config=config)
    routers = [group.router, *admin.setup(config)]
    for r in routers:
        dp.include_router(r)
    yield db, bot, dp, session
    for r in routers:  # роутеры модульные — отцепляем для следующего теста
        r._parent_router = None


_uid = iter(range(1, 10**6))


def user(uid: int, name: str = "Староста") -> User:
    first, *last = name.split()
    return User(id=uid, is_bot=False, first_name=first, last_name=" ".join(last) or None,
                username=f"user{uid}")


async def text(dp, bot, uid: int, txt: str, chat_id: int | None = None, chat_type="private"):
    chat_id = chat_id or uid
    msg = Message(message_id=next(_uid), date=datetime.now(),
                  chat=Chat(id=chat_id, type=chat_type), from_user=user(uid), text=txt)
    await dp.feed_update(bot, Update(update_id=next(_uid), message=msg))


async def press(dp, bot, uid: int, data: str, chat_id: int, name: str = "Староста"):
    msg = Message(message_id=next(_uid), date=datetime.now(),
                  chat=Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup"),
                  text="…")
    cb = CallbackQuery(id=str(next(_uid)), from_user=user(uid, name), chat_instance="x",
                       message=msg, data=data)
    await dp.feed_update(bot, Update(update_id=next(_uid), callback_query=cb))


def last_alert(session: FakeSession) -> str:
    return session.of(AnswerCallbackQuery)[-1].text or ""


def buttons(method) -> list[str]:
    markup = method.reply_markup
    if not markup:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


# ---------- юнит-тесты ----------

def test_parsers():
    today = date(2026, 9, 30)
    assert parse_times("9:00 13.40") == ["09:00", "13:40"]
    assert parse_times("25:00") is None
    assert parse_date("15.10", today) == date(2026, 10, 15)
    assert parse_date("01.09", today) == date(2027, 9, 1)
    assert parse_date("04.11.26", today) == date(2026, 11, 4)
    assert parse_date("31.02.2027", today) is None


def test_schedule_due_and_skip():
    db = Database(":memory:", "Europe/Moscow")
    tz = get_tz(db)
    db.add_schedule(2, "10:00")  # среда
    wed = datetime(2026, 9, 30, 10, 5, tzinfo=tz)
    assert due_slots(db, wed) == [(date(2026, 9, 30), "10:00")]
    assert due_slots(db, wed + timedelta(minutes=20)) == []  # опоздание больше окна
    assert due_slots(db, wed - timedelta(minutes=10)) == []
    db.add_skip_date("2026-09-30")
    assert due_slots(db, wed) == []
    db.add_extra_date("2026-09-30", "10:00")  # разовая дата важнее пропуска
    assert due_slots(db, wed) == [(date(2026, 9, 30), "10:00")]
    assert next_slot(db, wed) == datetime(2026, 10, 7, 10, 0, tzinfo=tz)


def test_mark_is_atomic():
    db = Database(":memory:")
    s = db.create_session(GROUP, "2026-09-30", "10:00", "slot", None)
    assert db.mark(s.id, 1) is True
    assert db.mark(s.id, 1) is False
    assert db.create_session(GROUP, "2026-09-30", "10:00", "slot", None) is None


# ---------- сценарии ----------

async def test_full_flow(env):
    db, bot, dp, session = env

    # не-админ не может привязать группу
    await text(dp, bot, 201, "/bind", GROUP, "supergroup")
    assert db.group_chat_id is None
    await text(dp, bot, ADMIN, "/bind", GROUP, "supergroup")
    assert db.group_chat_id == GROUP

    # регистрация
    s = await send_session(bot, db, date.today(), "10:00", slot="x")
    group_msg = session.of(SendMessage)[-1]
    assert group_msg.chat_id == GROUP
    assert buttons(group_msg) == [f"mark:{s.id}"]
    assert group_msg.reply_markup.inline_keyboard[0][0].style == "success"
    for uid, name in STUDENTS:
        await press(dp, bot, uid, "register", GROUP, name)
    assert len(db.list_students()) == 3

    # отметка + повторное нажатие
    await press(dp, bot, 201, f"mark:{s.id}", GROUP, "Иван Иванов")
    assert "записано" in last_alert(session)
    await press(dp, bot, 201, f"mark:{s.id}", GROUP, "Иван Иванов")
    assert "уже отметились" in last_alert(session)
    await press(dp, bot, 202, f"mark:{s.id}", GROUP, "Пётр Петров")
    assert set(db.marks(s.id)) == {201, 202}

    # счётчик в группе обновляется с задержкой
    await asyncio.sleep(2.2)
    edits = [e for e in session.of(EditMessageText) if e.chat_id == GROUP]
    assert edits and "Отметились: <b>2</b>" in edits[-1].text

    # карточка занятия у старосты
    await press(dp, bot, ADMIN, f"a:s:{s.id}", ADMIN)
    card = session.of(EditMessageText)[-1].text
    assert "Были: 2/3" in card and "Анна Смирнова" in card.split("Не было")[1]

    # закрытие — нажать больше нельзя
    await press(dp, bot, ADMIN, f"a:sc:{s.id}", ADMIN)
    await press(dp, bot, 203, f"mark:{s.id}", GROUP, "Анна Смирнова")
    assert "закрыта" in last_alert(session)

    # староста отмечает вручную
    await press(dp, bot, ADMIN, f"a:st:{s.id}:203:0", ADMIN)
    assert 203 in db.marks(s.id) and db.marks(s.id)[203].by_admin

    # не-админ в личке панель не видит
    before = len(session.of(SendMessage))
    await text(dp, bot, 201, "/menu")
    assert "Отмечайтесь кнопкой" in session.of(SendMessage)[before].text
    await press(dp, bot, 201, "a:menu", 201)
    assert len(session.of(SendMessage)) == before + 1  # кнопка проигнорирована


async def test_schedule_input(env):
    db, bot, dp, session = env
    await press(dp, bot, ADMIN, "a:schw", ADMIN)
    await press(dp, bot, ADMIN, "a:schwt:0", ADMIN)
    await press(dp, bot, ADMIN, "a:schwt:3", ADMIN)
    await press(dp, bot, ADMIN, "a:schwn", ADMIN)
    await text(dp, bot, ADMIN, "9:00 13:40")
    assert [(wd, t) for _, wd, t in db.list_schedule()] == [
        (0, "09:00"), (0, "13:40"), (3, "09:00"), (3, "13:40")]

    await press(dp, bot, ADMIN, "a:schd", ADMIN)
    await text(dp, bot, ADMIN, "15.10.2099 10:30\n16.10.2099 11:00")
    assert len(db.list_extra_dates()) == 2

    await press(dp, bot, ADMIN, "a:schs", ADMIN)
    await text(dp, bot, ADMIN, "29.12.2099-02.01.2100")
    assert len(db.list_skip_dates()) == 5

    await press(dp, bot, ADMIN, "a:schx", ADMIN)
    items = [b for b in buttons(session.of(EditMessageText)[-1]) if b.startswith("a:schx")]
    for data in items:
        await press(dp, bot, ADMIN, data, ADMIN)
    assert not db.list_schedule() and not db.list_extra_dates() and not db.list_skip_dates()


async def test_settings_input(env):
    db, bot, dp, session = env
    await press(dp, bot, ADMIN, "a:seti:timezone", ADMIN)
    await text(dp, bot, ADMIN, "Mars/Olympus")
    assert db.get("timezone") == "Europe/Moscow"
    await text(dp, bot, ADMIN, "Asia/Novosibirsk")
    assert db.get("timezone") == "Asia/Novosibirsk"
    await press(dp, bot, ADMIN, "a:seti:button", ADMIN)
    await text(dp, bot, ADMIN, "✅ Я на паре")
    assert db.get("button_text") == "✅ Я на паре"
    await press(dp, bot, ADMIN, "a:setwv:0", ADMIN)
    assert db.get_int("window_minutes") == 0


async def test_click_every_admin_button(env):
    """Обходит все кнопки панели и проверяет, что ни одна не падает."""
    db, bot, dp, session = env
    db.set("group_chat_id", GROUP)
    for uid, name in STUDENTS:
        db.upsert_student(uid, name, None)
    db.add_schedule(0, "09:00")
    db.add_extra_date("2099-01-01", "10:00")
    db.add_skip_date("2099-01-02")
    s = await send_session(bot, db, date.today(), "10:00")
    db.mark(s.id, 201)
    db.rename_student(202, "Петров П.")

    skip = {"a:sdy:", "a:udy:", "a:schxsa", "a:schxw", "a:schxd", "a:schxs:"}
    seen: set[str] = set()
    queue = ["a:menu"]
    errors = []

    dp.errors.register(lambda event: errors.append(event.exception))
    while queue:
        data = queue.pop()
        if data in seen or any(data.startswith(p) for p in skip):
            continue
        seen.add(data)
        n = len(session.calls)
        await press(dp, bot, ADMIN, data, ADMIN)
        for call in session.calls[n:]:
            if isinstance(call, (EditMessageText, SendMessage)) and call.chat_id == ADMIN:
                queue += [b for b in buttons(call) if b.startswith("a:")]
        assert session.of(AnswerCallbackQuery), data

    assert not errors, errors
    assert len(seen) > 40
    # удаление в самом конце
    await press(dp, bot, ADMIN, f"a:sdy:{s.id}", ADMIN)
    await press(dp, bot, ADMIN, "a:udy:201", ADMIN)
    assert db.get_session(s.id) is None and db.get_student(201) is None
    assert not errors, errors
