from __future__ import annotations

from ._connection import SQLiteConnectionMixin
from .execution_policy import ExecutionPolicyRepositoryImpl
from .guidance import GuidanceRepositoryImpl
from .intent import IntentRepositoryImpl
from .manual_delivery import ManualDeliveryRepositoryImpl
from .migrations import initialize_database
from .pnl import PnlRepositoryImpl
from .position_action import PositionActionRepositoryImpl
from .position_strategy import PositionStrategyRepositoryImpl
from .recovery import RecoveryRepositoryImpl
from .source import SourceRepositoryImpl


class SQLiteStore(
    SQLiteConnectionMixin,
    SourceRepositoryImpl,
    IntentRepositoryImpl,
    PositionActionRepositoryImpl,
    PositionStrategyRepositoryImpl,
    ManualDeliveryRepositoryImpl,
    GuidanceRepositoryImpl,
    ExecutionPolicyRepositoryImpl,
    PnlRepositoryImpl,
    RecoveryRepositoryImpl,
):
    async def initialize(self) -> None:
        await initialize_database(self._database_path)
