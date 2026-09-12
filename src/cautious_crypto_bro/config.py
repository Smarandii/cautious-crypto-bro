from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_api_id: int
    telegram_api_hash: str
    telegram_session_name: str = "cautious_crypto_bro"
    telegram_source_channels: list[str | int]

    telegram_bot_token: str
    telegram_approver_user_id: int
    telegram_approval_chat_id: int

    openrouter_api_key: str
    openrouter_model: str = "google/gemma-4-26b-a4b-it"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    bybit_api_key: str
    bybit_api_secret: str
    bybit_default_notional_usdt: float = 25.0

    database_path: Path = Path("data/cautious_crypto_bro.sqlite3")
    intent_max_age_seconds: int = 900
    log_level: str = "INFO"

    @field_validator("telegram_source_channels", mode="before")
    @classmethod
    def parse_channels(cls, value: object) -> object:
        if isinstance(value, str):
            channels: list[str | int] = []

            for raw in value.split(","):
                item = raw.strip()
                if not item:
                    continue

                if item.lstrip("-").isdigit():
                    channels.append(int(item))
                else:
                    channels.append(item.lstrip("@"))

            return channels

        return value

    @field_validator("telegram_source_channels")
    @classmethod
    def require_channels(
        cls,
        value: list[str | int],
    ) -> list[str | int]:
        if not value:
            raise ValueError("TELEGRAM_SOURCE_CHANNELS must contain at least one channel")
        return value

    @field_validator("bybit_default_notional_usdt")
    @classmethod
    def positive_notional(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("BYBIT_DEFAULT_NOTIONAL_USDT must be > 0")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
