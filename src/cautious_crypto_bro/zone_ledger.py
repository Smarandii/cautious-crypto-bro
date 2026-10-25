"""Immutable allocation evidence, not permission to mutate a net position."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from .domain import Side


class ZoneOrder(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    zone_id: UUID
    order_id: str = Field(min_length=1, pattern=r"^\S+$")
    order_link_id: str = Field(min_length=1, pattern=r"^\S+$")
    role: Literal["ENTRY", "EXIT", "STOP"]
    quantity: Decimal = Field(gt=0, allow_inf_nan=False)


class ZoneExecution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    execution_id: str = Field(min_length=1, pattern=r"^\S+$")
    order_id: str = Field(min_length=1, pattern=r"^\S+$")
    side: Side
    quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    closed_quantity: Decimal = Field(ge=0, allow_inf_nan=False)
    price: Decimal = Field(gt=0, allow_inf_nan=False)
    fee: Decimal = Field(default=Decimal("0"), allow_inf_nan=False)
    executed_at_ms: int = Field(gt=0)
