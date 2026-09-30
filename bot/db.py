"""Хранилище на SQLite: настройки, студенты, занятия, отметки, расписание."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS students (
    user_id      INTEGER PRIMARY KEY,
    full_name    TEXT NOT NULL,
    username     TEXT,
    custom_name  TEXT,
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    message_id  INTEGER,
    date        TEXT NOT NULL,          -- YYYY-MM-DD (местная дата)
    time        TEXT,                   -- HH:MM (местное время слота) или NULL
    slot        TEXT UNIQUE,            -- ключ слота расписания, защищает от повторной отправки
    created_at  TEXT NOT NULL,          -- UTC ISO
    close_at    TEXT,                   -- UTC ISO или NULL (без ограничения)
    closed      INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS attendance (
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL,
    marked_at   TEXT NOT NULL,          -- UTC ISO
    by_admin    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, user_id)
);
CREATE TABLE IF NOT EXISTS schedule (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    weekday  INTEGER NOT NULL,          -- 0 = понедельник
    time     TEXT NOT NULL,             -- HH:MM
    UNIQUE (weekday, time)
);
CREATE TABLE IF NOT EXISTS extra_dates (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    date  TEXT NOT NULL,
    time  TEXT NOT NULL,
    UNIQUE (date, time)
);
CREATE TABLE IF NOT EXISTS skip_dates (
    date TEXT PRIMARY KEY
);
"""

DEFAULT_SETTINGS: dict[str, str] = {
    "group_chat_id": "",
    "timezone": "",
    "title": "Отметка посещаемости",
    "button_text": "✅ Я был",
    "show_count": "1",       # показывать счётчик отметившихся в группе
    "show_names": "0",       # показывать список отметившихся в группе
    "window_minutes": "90",  # сколько минут открыта отметка (0 = без ограничения)
    "name_format": "full",   # full | username | both
    "stats_sort": "name",    # name | time
    "pin_message": "0",      # закреплять сообщение в группе
    "notify_on_close": "1",  # присылать старосте итог после закрытия отметки
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Student:
    user_id: int
    full_name: str
    username: str | None
    custom_name: str | None
    active: bool
    created_at: str

    @property
    def name(self) -> str:
        return self.custom_name or self.full_name


@dataclass
class Session:
    id: int
    chat_id: int
    message_id: int | None
    date: str
    time: str | None
    slot: str | None
    created_at: str
    close_at: str | None
    closed: bool


@dataclass
class Mark:
    user_id: int
    marked_at: str
    by_admin: bool


class Database:
    def __init__(self, path: str, default_tz: str = "Europe/Moscow"):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        for key, value in DEFAULT_SETTINGS.items():
            if key == "timezone":
                value = default_tz
            self.conn.execute(
                "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (key, value)
            )
        self.conn.commit()

    # ---------- настройки ----------
    def get(self, key: str) -> str:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else DEFAULT_SETTINGS.get(key, "")

    def get_bool(self, key: str) -> bool:
        return self.get(key) == "1"

    def get_int(self, key: str) -> int:
        try:
            return int(self.get(key))
        except ValueError:
            return 0

    def set(self, key: str, value: str | int | bool) -> None:
        if isinstance(value, bool):
            value = "1" if value else "0"
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    def toggle(self, key: str) -> bool:
        new = not self.get_bool(key)
        self.set(key, new)
        return new

    @property
    def group_chat_id(self) -> int | None:
        raw = self.get("group_chat_id")
        return int(raw) if raw else None

    # ---------- студенты ----------
    @staticmethod
    def _student(row: sqlite3.Row) -> Student:
        return Student(
            user_id=row["user_id"],
            full_name=row["full_name"],
            username=row["username"],
            custom_name=row["custom_name"],
            active=bool(row["active"]),
            created_at=row["created_at"],
        )

    def upsert_student(self, user_id: int, full_name: str, username: str | None) -> bool:
        """Добавляет студента или обновляет имя из Telegram. Возвращает True, если он новый."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO students(user_id, full_name, username, created_at) "
            "VALUES (?, ?, ?, ?)",
            (user_id, full_name, username, utcnow().isoformat()),
        )
        is_new = cur.rowcount == 1
        if not is_new:
            self.conn.execute(
                "UPDATE students SET full_name = ?, username = ? WHERE user_id = ?",
                (full_name, username, user_id),
            )
        self.conn.commit()
        return is_new

    def get_student(self, user_id: int) -> Student | None:
        row = self.conn.execute("SELECT * FROM students WHERE user_id = ?", (user_id,)).fetchone()
        return self._student(row) if row else None

    def list_students(self, include_inactive: bool = False) -> list[Student]:
        sql = "SELECT * FROM students"
        if not include_inactive:
            sql += " WHERE active = 1"
        rows = self.conn.execute(sql).fetchall()
        students = [self._student(r) for r in rows]
        students.sort(key=lambda s: s.name.lower())
        return students

    def rename_student(self, user_id: int, name: str | None) -> None:
        self.conn.execute("UPDATE students SET custom_name = ? WHERE user_id = ?", (name, user_id))
        self.conn.commit()

    def set_student_active(self, user_id: int, active: bool) -> None:
        self.conn.execute(
            "UPDATE students SET active = ? WHERE user_id = ?", (int(active), user_id)
        )
        self.conn.commit()

    def delete_student(self, user_id: int) -> None:
        self.conn.execute("DELETE FROM attendance WHERE user_id = ?", (user_id,))
        self.conn.execute("DELETE FROM students WHERE user_id = ?", (user_id,))
        self.conn.commit()

    # ---------- занятия ----------
    @staticmethod
    def _session(row: sqlite3.Row) -> Session:
        return Session(
            id=row["id"],
            chat_id=row["chat_id"],
            message_id=row["message_id"],
            date=row["date"],
            time=row["time"],
            slot=row["slot"],
            created_at=row["created_at"],
            close_at=row["close_at"],
            closed=bool(row["closed"]),
        )

    def create_session(
        self,
        chat_id: int,
        date: str,
        time: str | None,
        slot: str | None,
        close_at: datetime | None,
    ) -> Session | None:
        """Создаёт занятие. Для слота расписания возвращает None, если оно уже создано."""
        try:
            cur = self.conn.execute(
                "INSERT INTO sessions(chat_id, date, time, slot, created_at, close_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    chat_id,
                    date,
                    time,
                    slot,
                    utcnow().isoformat(),
                    close_at.astimezone(timezone.utc).isoformat() if close_at else None,
                ),
            )
        except sqlite3.IntegrityError:
            return None
        self.conn.commit()
        return self.get_session(cur.lastrowid)

    def set_session_message(self, session_id: int, message_id: int) -> None:
        self.conn.execute(
            "UPDATE sessions SET message_id = ? WHERE id = ?", (message_id, session_id)
        )
        self.conn.commit()

    def get_session(self, session_id: int) -> Session | None:
        row = self.conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return self._session(row) if row else None

    def list_sessions(self, limit: int = 1000, offset: int = 0) -> list[Session]:
        rows = self.conn.execute(
            "SELECT * FROM sessions ORDER BY date DESC, COALESCE(time, '') DESC, id DESC "
            "LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return [self._session(r) for r in rows]

    def count_sessions(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

    def sessions_to_close(self, now: datetime) -> list[Session]:
        rows = self.conn.execute(
            "SELECT * FROM sessions WHERE closed = 0 AND close_at IS NOT NULL AND close_at <= ?",
            (now.astimezone(timezone.utc).isoformat(),),
        ).fetchall()
        return [self._session(r) for r in rows]

    def set_session_closed(self, session_id: int, closed: bool, close_at: datetime | None = None) -> None:
        self.conn.execute(
            "UPDATE sessions SET closed = ?, close_at = ? WHERE id = ?",
            (
                int(closed),
                close_at.astimezone(timezone.utc).isoformat() if close_at else None,
                session_id,
            ),
        )
        self.conn.commit()

    def delete_session(self, session_id: int) -> None:
        self.conn.execute("DELETE FROM attendance WHERE session_id = ?", (session_id,))
        self.conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self.conn.commit()

    # ---------- отметки ----------
    def mark(self, session_id: int, user_id: int, by_admin: bool = False) -> bool:
        """Атомарно ставит отметку. Возвращает False, если отметка уже была."""
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO attendance(session_id, user_id, marked_at, by_admin) "
            "VALUES (?, ?, ?, ?)",
            (session_id, user_id, utcnow().isoformat(), int(by_admin)),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def unmark(self, session_id: int, user_id: int) -> None:
        self.conn.execute(
            "DELETE FROM attendance WHERE session_id = ? AND user_id = ?", (session_id, user_id)
        )
        self.conn.commit()

    def marks(self, session_id: int) -> dict[int, Mark]:
        rows = self.conn.execute(
            "SELECT user_id, marked_at, by_admin FROM attendance WHERE session_id = ? "
            "ORDER BY marked_at",
            (session_id,),
        ).fetchall()
        return {
            r["user_id"]: Mark(r["user_id"], r["marked_at"], bool(r["by_admin"])) for r in rows
        }

    def attendance_counts(self) -> dict[int, int]:
        rows = self.conn.execute(
            "SELECT user_id, COUNT(*) AS n FROM attendance GROUP BY user_id"
        ).fetchall()
        return {r["user_id"]: r["n"] for r in rows}

    def student_session_ids(self, user_id: int) -> set[int]:
        rows = self.conn.execute(
            "SELECT session_id FROM attendance WHERE user_id = ?", (user_id,)
        ).fetchall()
        return {r["session_id"] for r in rows}

    # ---------- расписание ----------
    def list_schedule(self) -> list[tuple[int, int, str]]:
        rows = self.conn.execute("SELECT id, weekday, time FROM schedule ORDER BY weekday, time")
        return [(r["id"], r["weekday"], r["time"]) for r in rows]

    def add_schedule(self, weekday: int, time: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO schedule(weekday, time) VALUES (?, ?)", (weekday, time)
        )
        self.conn.commit()

    def delete_schedule(self, item_id: int) -> None:
        self.conn.execute("DELETE FROM schedule WHERE id = ?", (item_id,))
        self.conn.commit()

    def list_extra_dates(self) -> list[tuple[int, str, str]]:
        rows = self.conn.execute("SELECT id, date, time FROM extra_dates ORDER BY date, time")
        return [(r["id"], r["date"], r["time"]) for r in rows]

    def add_extra_date(self, date: str, time: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO extra_dates(date, time) VALUES (?, ?)", (date, time)
        )
        self.conn.commit()

    def delete_extra_date(self, item_id: int) -> None:
        self.conn.execute("DELETE FROM extra_dates WHERE id = ?", (item_id,))
        self.conn.commit()

    def delete_past_extra_dates(self, today: str) -> None:
        self.conn.execute("DELETE FROM extra_dates WHERE date < ?", (today,))
        self.conn.execute("DELETE FROM skip_dates WHERE date < ?", (today,))
        self.conn.commit()

    def list_skip_dates(self) -> list[str]:
        return [r["date"] for r in self.conn.execute("SELECT date FROM skip_dates ORDER BY date")]

    def add_skip_date(self, date: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO skip_dates(date) VALUES (?)", (date,))
        self.conn.commit()

    def delete_skip_date(self, date: str) -> None:
        self.conn.execute("DELETE FROM skip_dates WHERE date = ?", (date,))
        self.conn.commit()

    def is_skip_date(self, date: str) -> bool:
        return (
            self.conn.execute("SELECT 1 FROM skip_dates WHERE date = ?", (date,)).fetchone()
            is not None
        )
