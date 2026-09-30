"""Кто был, кто пропустил, кто болел — единое правило для всего бота.

Отмечаются только отсутствующие: кнопка «Пропускаю» или больничный.
Все остальные студенты из списка считаются присутствовавшими.
Больничный важнее пропуска: если человек болеет, в ведомости будет «б».
"""

from .db import Database, Session

PRESENT = "present"
ABSENT = "absent"
SICK = "sick"


def session_statuses(db: Database, session: Session, user_ids=None) -> dict[int, str]:
    if user_ids is None:
        user_ids = [st.user_id for st in db.list_students()]
    absent = db.absences(session.id)
    sick = db.sick_on(session.date)
    result = {}
    for uid in user_ids:
        if uid in sick:
            result[uid] = SICK
        elif uid in absent:
            result[uid] = ABSENT
        else:
            result[uid] = PRESENT
    return result


def student_statuses(db: Database, user_id: int, sessions: list[Session]) -> dict[int, str]:
    """{session_id: статус} одного студента по списку занятий."""
    return {s.id: session_statuses(db, s, [user_id])[user_id] for s in sessions}


def totals(db: Database, sessions: list[Session]) -> dict[int, dict[str, int]]:
    """{user_id: {present, absent, sick}} по студентам из списка."""
    ids = [st.user_id for st in db.list_students()]
    result = {uid: {PRESENT: 0, ABSENT: 0, SICK: 0} for uid in ids}
    for s in sessions:
        for uid, status in session_statuses(db, s, ids).items():
            result[uid][status] += 1
    return result
