"""Own and supervise the configured isolated Demo account databases."""

import asyncio
import logging
from contextlib import AsyncExitStack
from decimal import Decimal
from pathlib import Path

from .bybit import BybitDemoExecutor
from .config import Settings
from .domain import TradeExecutionError
from .execution import ExecutionPlanner
from .isolated_demo_risk import IsolatedDemoPortfolio
from .portfolio_risk import PortfolioRiskUnavailable
from .ports import ExecutionCoordinatorStore
from .position_supervisor import PositionSupervisor
from .state_ownership import state_ownership
from .storage import SQLiteStore

logger = logging.getLogger(__name__)


class DemoAdmission:
    def __init__(self, mutation_lock: asyncio.Lock, worker_count: int) -> None:
        self.mutation_lock = mutation_lock
        self.worker_count = worker_count
        self.ready: set[int] = set()
        self.portfolio: IsolatedDemoPortfolio | None = None

    async def open_risk_usdt(self) -> Decimal:
        if self.portfolio is None or len(self.ready) != self.worker_count:
            raise PortfolioRiskUnavailable("Isolated Demo workers are not reconciled")
        try:
            return await self.portfolio.open_risk_usdt()
        except TradeExecutionError:
            raise PortfolioRiskUnavailable(
                "Combined Demo account risk is unavailable"
            ) from None


async def run_isolated_demo_workers(
    settings: Settings,
    primary: BybitDemoExecutor,
    primary_store: ExecutionCoordinatorStore,
    admission: DemoAdmission,
) -> None:
    configs = settings.isolated_demo_workers
    if not configs:
        return
    paths = tuple(config.database_path.resolve() for config in configs)
    expected = {config.uid for config in configs}
    cap = settings.demo_portfolio_stop_risk_cap_usdt
    if (
        len(configs) < 2
        or len(expected) != len(configs)
        or len(set(paths)) != len(paths)
        or settings.database_path.resolve() in paths
        or cap is None
        or not cap.is_finite()
    ):
        logger.error(
            "Isolated Demo workers require distinct UIDs and state paths and a portfolio cap"
        )
        return

    async with AsyncExitStack() as cleanup:
        planner = ExecutionPlanner()
        workers = []
        for config, path in zip(configs, paths, strict=True):
            executor = BybitDemoExecutor(
                api_key=config.api_key.get_secret_value(),
                api_secret=config.api_secret.get_secret_value(),
                planner=planner,
            )
            cleanup.callback(executor.close)
            workers.append((executor, SQLiteStore(path)))
        admission.portfolio = IsolatedDemoPortfolio(
            ((primary, primary_store), *workers),
            cap,
            mutation_lock=admission.mutation_lock,
        )

        while True:
            try:
                primary_uid = await primary.authenticated_uid()
                break
            except TradeExecutionError:
                logger.warning(
                    "Primary Demo identity unavailable; retrying worker startup in 30 seconds"
                )
                await asyncio.sleep(30)
        if primary_uid in expected:
            logger.error(
                "An isolated Demo worker is configured for the primary account"
            )
            return

        async with asyncio.TaskGroup() as tasks:
            for index, (config, path, worker) in enumerate(
                zip(configs, paths, workers, strict=True), start=1
            ):
                executor, store = worker
                supervisor = PositionSupervisor(
                    store=store,
                    executor=executor,
                    mutation_lock=admission.mutation_lock,
                    legacy_entry_ttl_minutes=settings.demo_entry_order_ttl_minutes,
                )
                tasks.create_task(
                    _run_worker(
                        index,
                        config.uid,
                        path,
                        executor,
                        store,
                        supervisor,
                        admission.ready,
                    )
                )


async def _run_worker(
    index: int,
    expected_uid: int,
    path: Path,
    executor: BybitDemoExecutor,
    store: SQLiteStore,
    supervisor: PositionSupervisor,
    ready: set[int],
) -> None:
    try:
        with state_ownership(path):
            initialized = False
            while True:
                try:
                    if await executor.authenticated_uid() != expected_uid:
                        logger.error(
                            "Isolated Demo worker %d has the wrong account", index
                        )
                        return
                    if not initialized:
                        await store.initialize_isolated_demo(expected_uid)
                        await store.quarantine_interrupted_executions()
                        initialized = True
                    await executor.account_state()
                    await supervisor.reconcile_once()
                    break
                except TradeExecutionError:
                    logger.warning(
                        "Isolated Demo worker %d startup unavailable; retrying in 30 seconds",
                        index,
                    )
                    await asyncio.sleep(30)
            logger.info("Isolated Demo worker %d reconciled", index)
            ready.add(index)
            await supervisor.run()
    except Exception as exc:
        logger.error("Isolated Demo worker %d stopped (%s)", index, type(exc).__name__)
    finally:
        ready.discard(index)
