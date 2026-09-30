"""Резервные копии базы: отправка старосте в личку и восстановление из файла."""

import logging
import os
import sqlite3
import tempfile
from datetime import datetime, time

from aiogram import Bot
from aiogram.types import BufferedInputFile

from .db import Database
from .excel import build_workbook
from .utils import WEEKDAYS_FULL, get_tz, now_local

log = logging.getLogger(__name__)

BACKUP_MODES = {"daily": "каждый день", "weekly": "раз в неделю", "off": "выключено"}


def dump_database(db: Database) -> bytes:
    """Согласованный снимок базы (безопасно даже во время работы бота)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "backup.db")
        target = sqlite3.connect(path)
        try:
            db.conn.backup(target)
        finally:
            target.close()
        with open(path, "rb") as f:
            return f.read()


def backup_summary(db: Database) -> str:
    q = db.conn.execute
    students = q("SELECT COUNT(*) FROM students WHERE active = 1 AND in_chat = 1").fetchone()[0]
    sessions = q("SELECT COUNT(*) FROM sessions").fetchone()[0]
    absences = q("SELECT COUNT(*) FROM absences").fetchone()[0]
    sick = q("SELECT COUNT(*) FROM sick_leaves").fetchone()[0]
    return (
        f"студентов: {students}, занятий: {sessions}, пропусков: {absences}, "
        f"больничных: {sick}"
    )


async def send_backup(bot: Bot, db: Database, admin_ids: frozenset[int], reason: str) -> int:
    """Отправляет копию (база + Excel) всем старостам. Возвращает, скольким дошло."""
    now = now_local(db)
    stamp = f"{now:%Y-%m-%d_%H%M}"
    data = dump_database(db)
    try:
        excel = build_workbook(db)
    except Exception:  # ведомость — приятное дополнение, база важнее
        log.exception("Не удалось собрать Excel для резервной копии")
        excel = None

    caption = (
        f"💾 <b>Резервная копия</b> — {reason}\n"
        f"{now:%d.%m.%Y %H:%M}: {backup_summary(db)}.\n\n"
        "Сохраните этот файл. Чтобы восстановить данные: /menu → 💾 Резервные копии → "
        "♻️ Восстановить, и отправьте боту этот файл."
    )
    delivered = 0
    for uid in admin_ids:
        try:
            await bot.send_document(
                uid, BufferedInputFile(data, filename=f"vedomost_backup_{stamp}.db"),
                caption=caption,
            )
            if excel:
                await bot.send_document(
                    uid, BufferedInputFile(excel, filename=f"vedomost_{stamp}.xlsx"),
                    caption="📥 Ведомость на момент копии (для просмотра).",
                    disable_notification=True,
                )
            delivered += 1
        except Exception as e:
            log.warning("Не удалось отправить копию старосте %s: %s", uid, e)
    db.set("backup_last", now.isoformat())
    return delivered


def backup_due(db: Database, now: datetime) -> bool:
    mode = db.get("backup_mode")
    if mode not in ("daily", "weekly"):
        return False
    hh, mm = map(int, (db.get("backup_time") or "23:00").split(":"))
    at = datetime.combine(now.date(), time(hh, mm), get_tz(db))
    if now < at:
        return False
    if mode == "weekly" and now.weekday() != db.get_int("backup_weekday"):
        return False
    last = db.get("backup_last")
    if last and datetime.fromisoformat(last).astimezone(get_tz(db)) >= at:
        return False  # сегодняшняя (или более свежая) копия уже есть
    return True


def describe_schedule(db: Database) -> str:
    mode = db.get("backup_mode")
    if mode == "daily":
        return f"каждый день в {db.get('backup_time')}"
    if mode == "weekly":
        day = WEEKDAYS_FULL[db.get_int("backup_weekday")].lower()
        return f"раз в неделю: {day}, {db.get('backup_time')}"
    return "выключено"


class RestoreError(Exception):
    pass


def _open_backup(data: bytes, tmp: str) -> sqlite3.Connection:
    if not data.startswith(b"SQLite format 3\x00"):
        raise RestoreError("Это не файл резервной копии бота.")
    path = os.path.join(tmp, "restore.db")
    with open(path, "wb") as f:
        f.write(data)
    src = sqlite3.connect(path)
    try:
        if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RestoreError("Файл повреждён.")
        tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        # копии старых версий без новых таблиц тоже принимаем — их обновит миграция
        if not {"settings", "students", "sessions"} <= tables:
            raise RestoreError("В файле нет данных бота посещаемости.")
    except sqlite3.DatabaseError as e:
        src.close()
        raise RestoreError(f"Не удалось прочитать файл: {e}") from e
    except RestoreError:
        src.close()
        raise
    return src


def inspect_backup(data: bytes) -> str:
    """Что лежит в файле копии — чтобы староста проверил перед восстановлением."""
    with tempfile.TemporaryDirectory() as tmp:
        src = _open_backup(data, tmp)
        try:
            students = src.execute("SELECT COUNT(*) FROM students").fetchone()[0]
            row = src.execute("SELECT COUNT(*), MIN(date), MAX(date) FROM sessions").fetchone()
        finally:
            src.close()
    text = f"студентов: {students}, занятий: {row[0]}"
    if row[0]:
        text += f" (с {row[1][8:10]}.{row[1][5:7]}.{row[1][:4]} по {row[2][8:10]}.{row[2][5:7]}.{row[2][:4]})"
    return text


def restore_database(db: Database, data: bytes) -> str:
    """Заменяет текущие данные содержимым файла копии. Возвращает сводку."""
    with tempfile.TemporaryDirectory() as tmp:
        src = _open_backup(data, tmp)
        try:
            src.backup(db.conn)
        finally:
            src.close()
    db.upgrade()  # досоздать новые таблицы и перенести данные старых версий
    return backup_summary(db)
