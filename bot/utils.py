import html
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .db import Database, Student

WEEKDAYS_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
MONTHS = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]
MONTHS_GEN = [
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
]
WEEKDAYS_FULL = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

NAME_FORMATS = {
    "full": "Имя Фамилия",
    "username": "@username",
    "both": "Имя (@username)",
}


def esc(text: str) -> str:
    return html.escape(text, quote=False)


def get_tz(db: Database) -> ZoneInfo:
    try:
        return ZoneInfo(db.get("timezone") or "Europe/Moscow")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("Europe/Moscow")


def is_valid_tz(name: str) -> bool:
    try:
        ZoneInfo(name)
        return True
    except (ZoneInfoNotFoundError, ValueError):
        return False


def now_local(db: Database) -> datetime:
    return datetime.now(get_tz(db))


def to_local(db: Database, iso_utc: str) -> datetime:
    return datetime.fromisoformat(iso_utc).astimezone(get_tz(db))


def fmt_date(iso_date: str, with_weekday: bool = False, full_weekday: bool = False) -> str:
    d = date.fromisoformat(iso_date)
    text = d.strftime("%d.%m.%Y")
    if with_weekday:
        wd = (WEEKDAYS_FULL if full_weekday else WEEKDAYS_SHORT)[d.weekday()]
        text = f"{wd}, {text}" if full_weekday else f"{wd} {text}"
    return text


def fmt_long_date(iso_date: str) -> str:
    """«Среда, 30 сентября 2026»."""
    d = date.fromisoformat(iso_date)
    return f"{WEEKDAYS_FULL[d.weekday()]}, {d.day} {MONTHS_GEN[d.month - 1]} {d.year}"


def fmt_short_date(iso_date: str) -> str:
    d = date.fromisoformat(iso_date)
    return f"{d.strftime('%d.%m')} {WEEKDAYS_SHORT[d.weekday()]}"


_TIME_RE = re.compile(r"^(\d{1,2})[:.](\d{2})$")
_DATE_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?$")


def parse_time(text: str) -> str | None:
    m = _TIME_RE.match(text.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return f"{h:02d}:{mi:02d}"


def parse_date(text: str, today: date) -> date | None:
    """Разбирает «15.10», «15.10.26», «15.10.2026». Без года — ближайшая будущая дата."""
    m = _DATE_RE.match(text.strip())
    if not m:
        return None
    day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
    try:
        if year:
            y = int(year)
            if y < 100:
                y += 2000
            return date(y, month, day)
        d = date(today.year, month, day)
        if d < today:
            d = date(today.year + 1, month, day)
        return d
    except ValueError:
        return None


def parse_times(text: str) -> list[str] | None:
    parts = [p for p in re.split(r"[\s,;]+", text.strip()) if p]
    if not parts:
        return None
    times = [parse_time(p) for p in parts]
    if any(t is None for t in times):
        return None
    return sorted(set(times))  # type: ignore[arg-type]


def date_range(start: date, end: date) -> list[date]:
    if end < start:
        start, end = end, start
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def student_name(student: Student, name_format: str) -> str:
    if student.custom_name:
        base = student.custom_name
    else:
        base = student.full_name
    if name_format == "username" and student.username:
        return f"@{student.username}"
    if name_format == "both" and student.username:
        return f"{base} (@{student.username})"
    return base


def pct(part: int, total: int) -> str:
    if total == 0:
        return "—"
    return f"{round(part * 100 / total)}%"
