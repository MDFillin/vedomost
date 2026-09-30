import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    token: str
    admin_ids: frozenset[int]
    db_path: str
    default_tz: str


def load_config() -> Config:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN не задан (см. .env.example)")

    raw_admins = os.getenv("ADMIN_IDS", "")
    admin_ids = frozenset(int(x) for x in raw_admins.replace(" ", "").split(",") if x)
    if not admin_ids:
        raise SystemExit("ADMIN_IDS не задан — укажите свой Telegram ID (см. .env.example)")

    tz = os.getenv("TIMEZONE", "Europe/Moscow").strip()
    ZoneInfo(tz)  # проверка, что пояс существует

    return Config(
        token=token,
        admin_ids=admin_ids,
        db_path=os.getenv("DB_PATH", "data/vedomost.db"),
        default_tz=tz,
    )
