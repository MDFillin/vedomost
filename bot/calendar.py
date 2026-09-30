"""Инлайн-календарь для выбора дат."""

import calendar as _cal
import re
from datetime import date

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .utils import MONTHS, WEEKDAYS_SHORT


_DATE_TOKEN = re.compile(r"\d{1,2}\.\d{1,2}(?:\.\d{2,4})?")


def calendar_rows(
    year: int,
    month: int,
    *,
    min_day: date,
    max_day: date,
    today: date,
    selected: date | None = None,
    prefix: str = "sk",
) -> list[list[InlineKeyboardButton]]:
    def btn(text: str, data: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=data)

    noop = f"{prefix}:noop"
    prev_m = (year, month - 1) if month > 1 else (year - 1, 12)
    next_m = (year, month + 1) if month < 12 else (year + 1, 1)
    can_prev = date(prev_m[0], prev_m[1], _cal.monthrange(*prev_m)[1]) >= min_day
    can_next = date(next_m[0], next_m[1], 1) <= max_day

    rows = [[
        btn("◀️", f"{prefix}:m:{prev_m[0]}-{prev_m[1]:02d}") if can_prev else btn(" ", noop),
        btn(f"{MONTHS[month - 1]} {year}", noop),
        btn("▶️", f"{prefix}:m:{next_m[0]}-{next_m[1]:02d}") if can_next else btn(" ", noop),
    ]]
    rows.append([btn(d, noop) for d in WEEKDAYS_SHORT])
    for week in _cal.Calendar(firstweekday=0).monthdayscalendar(year, month):
        row = []
        for day in week:
            if day == 0:
                row.append(btn(" ", noop))
                continue
            d = date(year, month, day)
            if d < min_day or d > max_day:
                row.append(btn("·", noop))
                continue
            label = str(day)
            if d == selected:
                label = f"[{day}]"
            elif d == today:
                label = f"•{day}"
            row.append(btn(label, f"{prefix}:d:{d.isoformat()}"))
        rows.append(row)
    return rows


def calendar_kb(*args, extra: list[list[InlineKeyboardButton]] | None = None, **kwargs) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=calendar_rows(*args, **kwargs) + (extra or []))


def nearest_date(token: str, today: date) -> date | None:
    """«28.09» → ближайшая к сегодняшнему дню такая дата (в прошлом или будущем)."""
    parts = token.split(".")
    try:
        day, month = int(parts[0]), int(parts[1])
        if len(parts) == 3:
            year = int(parts[2])
            return date(year + 2000 if year < 100 else year, month, day)
        candidates = []
        for y in (today.year - 1, today.year, today.year + 1):
            try:
                candidates.append(date(y, month, day))
            except ValueError:
                pass
        return min(candidates, key=lambda d: abs((d - today).days)) if candidates else None
    except (ValueError, IndexError):
        return None


def parse_date_tokens(text: str, today: date) -> list[date] | None:
    tokens = _DATE_TOKEN.findall(text)
    if not tokens:
        return None
    dates = [nearest_date(t, today) for t in tokens]
    if any(d is None for d in dates):
        return None
    return dates  # type: ignore[return-value]
