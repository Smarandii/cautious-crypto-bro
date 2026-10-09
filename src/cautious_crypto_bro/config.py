from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .domain import AutoApprovalMode


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
    telegram_startup_lookback_hours: int = Field(
        default=5,
        ge=0,
        le=168,
    )
    # Live Telegram pushes are dropped often enough to miss real signals,
    # so a poll reconciles each channel against history.
    telegram_catchup_interval_seconds: int = Field(
        default=60,
        ge=15,
        le=3600,
    )

    telegram_bot_token: str
    telegram_approver_user_id: int
    telegram_approval_chat_id: int

    llm_providers: list[
        Literal[
            "openrouter",
            "opencode_go",
        ]
    ] = Field(
        default_factory=lambda: [
            "openrouter",
        ],
        min_length=1,
        max_length=2,
    )

    llm_evaluation_cache_hours: int = Field(
        default=6,
        ge=1,
        le=168,
        validation_alias=AliasChoices(
            "LLM_EVALUATION_CACHE_HOURS",
            "OPENROUTER_EVALUATION_CACHE_HOURS",
        ),
    )

    openrouter_api_key: str | None = None
    openrouter_model: str = "openai/gpt-5.6-luna"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_inference_timeout_seconds: float = Field(
        default=60,
        ge=5,
        le=120,
    )
    openrouter_inference_max_attempts: int = Field(
        default=3,
        ge=1,
        le=3,
    )
    openrouter_provider_cooldown_hours: int = Field(
        default=12,
        ge=1,
        le=168,
    )

    opencode_go_api_key: str | None = None
    opencode_go_model: str = "gpt-5.6-luna"
    opencode_go_base_url: str = "https://opencode.ai/zen/go/v1"
    opencode_go_inference_timeout_seconds: float = Field(
        default=60,
        ge=5,
        le=120,
    )
    opencode_go_inference_max_attempts: int = Field(
        default=3,
        ge=1,
        le=3,
    )

    redis_url: str = "redis://redis:6379/0"
    redis_max_connections: int = Field(
        default=64,
        ge=1,
        le=512,
    )
    redis_pool_timeout_seconds: float = Field(
        default=5,
        gt=0,
        le=60,
    )

    bybit_api_key: str
    bybit_api_secret: str

    database_path: Path = Path("data/cautious_crypto_bro.sqlite3")
    source_processing_lease_seconds: int = Field(
        default=300,
        ge=60,
        le=3600,
    )
    # Kept for env compatibility; applies only to position actions, not OPEN.
    intent_max_age_seconds: int = Field(default=900, gt=0)

    auto_approval_mode: AutoApprovalMode = AutoApprovalMode.DISABLED

    # Demo-only sizing experiment. A value of 0.10 means new LONG plans use
    # 10% of their normal risk budget; 1.0 leaves sizing unchanged.
    demo_long_risk_multiplier: Decimal = Field(
        default=Decimal("1"),
        gt=0,
        le=1,
    )

    # Randomized LONG-exit control share; only used with the long_015 profile.
    demo_long_exit_control_fraction: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        le=1,
    )

    # Randomized fraction of auto-approved Demo LONG signals assigned to skip.
    demo_long_participation_skip_fraction: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        le=1,
    )

    # Caps combined exchange stop risk for new Demo entries. Existing positions
    # are never resized; unset disables the portfolio-level cap.
    demo_portfolio_stop_risk_cap_usdt: Decimal | None = Field(
        default=None,
        gt=0,
    )

    # Applies only to newly created Strategy V2 plans; zero leaves entries GTC.
    demo_entry_order_ttl_minutes: int = Field(default=0, ge=0)

    # Applies to new Demo plans only; existing execution plans retain their
    # serialized Strategy V2 policy.
    demo_exit_profile: Literal[
        "baseline",
        "payoff_challenger",
        "payoff_early_trail",
        "payoff_early_tight_trail",
        "payoff_early_tight_trail_long_015",
    ] = "baseline"

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    # BaseSettings resolves required values from the environment at runtime.
    return Settings()  # pyright: ignore[reportCallIssue]
