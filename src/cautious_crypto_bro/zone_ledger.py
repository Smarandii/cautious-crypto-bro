"""Immutable allocation evidence, not permission to mutate a net position."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .domain import Side


class ZoneOrder(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    zone_id: UUID
    order_id: str = Field(min_length=1, pattern=r"^\S+$")
    order_link_id: str = Field(pattern=r"^\S*$")
    parent_order_link_id: str = Field(default="", pattern=r"^\S*$")
    role: Literal["ENTRY", "EXIT", "STOP"]
    quantity: Decimal = Field(gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def explicit_native_parent(self) -> ZoneOrder:
        # Zen of Python: "Explicit is better than implicit." Native children
        # have no submitted client link; never invent one to imply ownership.
        if self.parent_order_link_id:
            if self.order_link_id or self.role == "ENTRY":
                raise ValueError(
                    "Native child requires absent client link and closing role"
                )
        elif not self.order_link_id:
            raise ValueError("Order requires a client link or explicit native parent")
        return self


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
