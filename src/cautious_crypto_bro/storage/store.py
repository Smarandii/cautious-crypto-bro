from __future__ import annotations

from ._connection import SQLiteConnectionMixin
from .attention import OperatorAttentionRepositoryImpl
from .auto_outcome import AutoOutcomeRepositoryImpl
from .entry_review import EntryReviewRepositoryImpl
from .execution_policy import ExecutionPolicyRepositoryImpl
from .guidance import GuidanceRepositoryImpl
from .intent import IntentRepositoryImpl
from .manual_delivery import ManualDeliveryRepositoryImpl
from .migrations import initialize_database, initialize_isolated_demo_database
from .planning_candidate import PlanningCandidateRepositoryImpl
from .pnl import PnlRepositoryImpl
from .position_action import PositionActionRepositoryImpl
from .position_strategy import PositionStrategyRepositoryImpl
from .recovery import RecoveryRepositoryImpl
from .zone import ZoneLedgerRepositoryImpl
from .zone_history import ZoneHistoryRepositoryImpl


class SQLiteStore(
    SQLiteConnectionMixin,
    EntryReviewRepositoryImpl,
    OperatorAttentionRepositoryImpl,
    AutoOutcomeRepositoryImpl,
    IntentRepositoryImpl,
    PositionActionRepositoryImpl,
    PositionStrategyRepositoryImpl,
    ManualDeliveryRepositoryImpl,
    GuidanceRepositoryImpl,
    ExecutionPolicyRepositoryImpl,
    PnlRepositoryImpl,
    PlanningCandidateRepositoryImpl,
    RecoveryRepositoryImpl,
    ZoneLedgerRepositoryImpl,
    ZoneHistoryRepositoryImpl,
):
    async def initialize(self) -> None:
        await initialize_database(self._database_path)

    async def initialize_isolated_demo(self, verified_uid: int) -> None:
        await initialize_isolated_demo_database(self._database_path, verified_uid)
