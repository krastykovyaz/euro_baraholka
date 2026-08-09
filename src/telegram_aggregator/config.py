from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

from dotenv import load_dotenv


@dataclass(slots=True, frozen=True)
class Settings:
    telegram_api_id: int
    telegram_api_hash: str
    telegram_session: str
    telegram_bot_session: str
    telegram_bot_token: str | None
    source_chat: str
    target_channel: str
    admin_telegram_id: int | None
    gemini_api_key: str
    gemini_model: str
    mode: str
    initial_messages: int
    max_posts_per_run: int
    max_age_days: int
    max_flood_wait_seconds: int
    send_throttle_seconds: int
    database_path: Path

    @property
    def is_test(self) -> bool:
        return self.mode.upper() == "TEST"

    @property
    def is_live(self) -> bool:
        return self.mode.upper() == "LIVE"


def load_settings() -> Settings:
    # override=True: значения из .env всегда побеждают уже выставленные
    # переменные окружения шелла/systemd.
    load_dotenv(override=True)

    def _required(name: str) -> str:
        value = os.getenv(name, "").strip()
        if not value:
            raise ValueError(f"Missing required environment variable: {name}")
        return value

    telegram_session = _required("TELEGRAM_SESSION")
    # ВАЖНО: у юзер-сессии и бот-сессии должны быть РАЗНЫЕ session-файлы.
    # Один и тот же файл нельзя одновременно авторизовать и как юзера
    # (по телефону), и как бота (по токену) — Telethon в этом случае
    # молча оставит уже существующую авторизацию (юзера) и проигнорирует
    # bot_token. Отсюда и была путаница "бот работает от лица юзера".
    telegram_bot_session = os.getenv("TELEGRAM_BOT_SESSION", "").strip() or f"{telegram_session}_bot"

    return Settings(
        telegram_api_id=int(_required("TELEGRAM_API_ID")),
        telegram_api_hash=_required("TELEGRAM_API_HASH"),
        telegram_session=telegram_session,
        telegram_bot_session=telegram_bot_session,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or None,
        source_chat=_required("SOURCE_CHAT"),
        target_channel=_required("TARGET_CHANNEL"),
        admin_telegram_id=int(os.getenv("ADMIN_TELEGRAM_ID", "0")) or None,
        gemini_api_key=_required("GEMINI_API_KEY"),
        gemini_model=os.getenv("GEMINI_MODEL", "auto").strip() or "auto",
        mode=os.getenv("MODE", "TEST").strip() or "TEST",
        initial_messages=int(os.getenv("INITIAL_MESSAGES", "100")),
        max_posts_per_run=int(os.getenv("MAX_POSTS_PER_RUN", "20")),
        max_age_days=int(os.getenv("MAX_AGE_DAYS", "3")),
        max_flood_wait_seconds=int(os.getenv("MAX_FLOOD_WAIT_SECONDS", "60")),
        send_throttle_seconds=int(os.getenv("SEND_THROTTLE_SECONDS", "8")),
        database_path=Path(os.getenv("DATABASE_PATH", "data/aggregator.sqlite3")),
    )