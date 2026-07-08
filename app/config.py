"""Настройки приложения. Все секреты берутся только из .env / переменных окружения."""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    anthropic_api_key: str
    openai_api_key: str
    model: str
    parser_temperature: float
    channel_temperature: float
    editor_temperature: float
    debounce_seconds: int
    allowed_user_ids: tuple[int, ...]


def _parse_user_ids(raw: str) -> tuple[int, ...]:
    ids = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part:
            ids.append(int(part))
    return tuple(ids)


def load_settings() -> Settings:
    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", "").strip(),
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6").strip(),
        parser_temperature=float(os.getenv("PARSER_TEMPERATURE", "0.2")),
        channel_temperature=float(os.getenv("CHANNEL_TEMPERATURE", "0.6")),
        editor_temperature=float(os.getenv("EDITOR_TEMPERATURE", "0.2")),
        debounce_seconds=int(os.getenv("DEBOUNCE_SECONDS", "20")),
        allowed_user_ids=_parse_user_ids(os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")),
    )


def require(settings: Settings, *fields: str) -> None:
    """Проверяет, что нужные секреты заполнены, иначе бросает понятную ошибку."""
    names = {
        "telegram_bot_token": "TELEGRAM_BOT_TOKEN",
        "anthropic_api_key": "ANTHROPIC_API_KEY",
        "openai_api_key": "OPENAI_API_KEY",
    }
    missing = [names[f] for f in fields if not getattr(settings, f)]
    if missing:
        raise RuntimeError(
            "Не заполнены переменные окружения: " + ", ".join(missing)
            + ". Скопируйте .env.example в .env и впишите значения."
        )
