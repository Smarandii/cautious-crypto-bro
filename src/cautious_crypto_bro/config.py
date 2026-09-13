from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_api_id: int
    telegram_api_hash: str
    telegram_session_name: str = "cautious_crypto_bro"
    telegram_source_channels: list[str | int] = Field(min_length=1)

    telegram_bot_token: str
    telegram_approver_user_id: int
    telegram_approval_chat_id: int

    openrouter_api_key: str
    openrouter_model: str = "google/gemma-4-26b-a4b-it"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_inference_timeout_seconds: float = Field(
        default=45,
        ge=5,
        le=120,
    )
    openrouter_inference_max_attempts: int = Field(
        default=2,
        ge=1,
        le=3,
    )

    bybit_api_key: str
    bybit_api_secret: str

    database_path: Path = Path("data/cautious_crypto_bro.sqlite3")
    intent_max_age_seconds: int = Field(default=900, gt=0)
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
