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
    # Необязательно: ключи с my.telegram.org, чтобы бот сам считывал всех участников чата
    api_id: int | None = None
    api_hash: str | None = None

    @property
    def can_read_members(self) -> bool:
        return bool(self.api_id and self.api_hash)


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

    api_id = os.getenv("API_ID", "").strip()
    api_hash = os.getenv("API_HASH", "").strip()

    return Config(
        token=token,
        admin_ids=admin_ids,
        db_path=os.getenv("DB_PATH", "data/vedomost.db"),
        default_tz=tz,
        api_id=int(api_id) if api_id else None,
        api_hash=api_hash or None,
    )
