"""A real terminated process leaves a journal recoverable through the real adapter."""

import asyncio
import json
import os
import subprocess
import sys
from contextlib import ExitStack
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from test_demo_e2e import demo
from test_remaining_concerns import executor_for
from test_zone_probe_integration import PairedExchange

from cautious_crypto_bro.domain import Side

pytestmark = pytest.mark.integration
ROOT = Path(__file__).parents[1]
WORKER = """
import asyncio,json,os,sys
from pathlib import Path
from types import SimpleNamespace
from test_demo_e2e import demo
from test_remaining_concerns import executor_for
from test_zone_probe_integration import PairedExchange
from cautious_crypto_bro.domain import Side
directory=Path(sys.argv[1]);side=Side(sys.argv[2])
journal=Path(sys.argv[3]) if len(sys.argv)>3 else directory/'ownership.json'
exchange=PairedExchange(side,False)
abrupt_exit=os._exit
def crash(code):
    (directory/'broker.json').write_text(json.dumps({
        'orders':exchange.orders,'executions':exchange.executions,
        'size':str(exchange.size),'avg':str(exchange.avg),'stop':str(exchange.stop)
    }))
    abrupt_exit(code)
demo.os._exit=crash
os.environ['CCB_PROBE_EVIDENCE_DIR']=str(directory)
with executor_for(exchange) as executor:
    demo.executor=lambda:executor
    asyncio.run(demo.main(SimpleNamespace(resume_db=None,execute_demo=True,
        zone_recovery_worker=side.value,ownership_journal=journal)))
"""


def reloaded_exchange(tmp_path, side):
    state = json.loads((tmp_path / "broker.json").read_text())
    exchange = PairedExchange(side, False)
    exchange.orders, exchange.executions = state["orders"], state["executions"]
    exchange.size, exchange.avg, exchange.stop = map(
        Decimal, (state["size"], state["avg"], state["stop"])
    )
    return exchange


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
@pytest.mark.parametrize(
    "outcome", ["clean", "foreign_fill", "unknown_order", "close_submitted"]
)
def test_crashed_worker_ownership_recovers_without_replaying_entries(
    tmp_path, side, outcome
):
    started = int(datetime.now(UTC).timestamp() * 1000)
    result = subprocess.run(
        [sys.executable, "-c", WORKER, str(tmp_path), side.value],
        env={
            **os.environ,
            "PYTHONPATH": os.pathsep.join(
                str(ROOT / p) for p in ("src", "tests", "scripts")
            ),
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 86, result.stderr
    assert '"journal_flushed": true' in result.stdout
    assert '"cleanup"' not in result.stdout
    journal = tmp_path / "ownership.json"
    saved = json.loads(journal.read_text())
    assert len(saved["submitted_links"]) == 2
    assert len(saved["observations"]["zone_entries"]["children"]) == 4
    exchange = reloaded_exchange(tmp_path, side)
    if outcome == "foreign_fill":
        exchange.executions.append(
            {
                **exchange.executions[0],
                "execId": "foreign",
                "orderId": "foreign",
                "orderLinkId": "foreign",
            }
        )
    if outcome == "unknown_order":
        child = next(o for o in exchange.orders.values() if o.get("parentOrderLinkId"))
        exchange.orders["foreign"] = {
            **child,
            "orderId": "foreign",
            "parentOrderLinkId": "foreign",
            "orderLinkId": "ccb-action-foreign",
        }
    if outcome == "close_submitted":
        saved["cleanup_close_submitted"] = True
        journal.write_text(json.dumps(saved))

    async def recover(executor):
        if outcome in {"foreign_fill", "unknown_order"}:
            with pytest.raises(
                AssertionError,
                match="Foreign or unattributed|Unknown probe-symbol order",
            ):
                await demo.ProbeOwnership.recover(
                    executor, journal, side, started_after_ms=started
                )
            return
        owner = await demo.ProbeOwnership.recover(
            executor, journal, side, started_after_ms=started
        )
        try:
            assert owner.links == set(saved["submitted_links"])
            account = await executor.account_state()
            expected = saved["observations"]["zone_entries"]
            actual = demo.pair_evidence(
                account, {c["parent_link"] for c in expected["children"]}
            )
            assert actual["net_quantity"] == expected["net_quantity"]
            assert sorted(actual["children"], key=lambda o: o["order_id"]) == sorted(
                expected["children"], key=lambda o: o["order_id"]
            )
            if outcome == "close_submitted":
                with pytest.raises(RuntimeError, match="Cleanup unverified"):
                    await demo.cleanup(executor, side, "recovered-", ownership=owner)
            else:
                await demo.cleanup(executor, side, "recovered-", ownership=owner)
        finally:
            owner.restore()

    with executor_for(exchange) as executor:
        asyncio.run(recover(executor))
    creates = [body for path, body in exchange.requests if path == "/v5/order/create"]
    assert len(creates) == int(outcome == "clean")
    assert all(body["reduceOnly"] is True for body in creates)
    assert (exchange.size == 0) is (outcome == "clean")


@pytest.mark.parametrize("outcome", ["clean", "missing_child", "timeout"])
def test_orchestrator_waits_for_worker_death_then_recovers_and_cleans(
    tmp_path, monkeypatch, capsys, outcome
):
    monkeypatch.setenv("CCB_PROBE_EVIDENCE_DIR", str(tmp_path))
    spawn = asyncio.create_subprocess_exec
    wait_for = asyncio.wait_for
    workers, exchanges = [], []
    with ExitStack() as stack:

        async def worker(*args, **kwargs):
            assert "--zone-recovery-worker" in args
            program = WORKER if outcome != "timeout" else "import time; time.sleep(5)"
            process = await spawn(
                sys.executable,
                "-c",
                program,
                str(tmp_path),
                "LONG",
                args[-1],
                env={
                    **os.environ,
                    "PYTHONPATH": os.pathsep.join(
                        str(ROOT / p) for p in ("src", "tests", "scripts")
                    ),
                },
                **kwargs,
            )
            workers.append(process)
            return process

        def recovery_executor():
            assert workers and workers[0].returncode is not None
            exchange = (
                reloaded_exchange(tmp_path, Side.LONG)
                if (tmp_path / "broker.json").exists()
                else PairedExchange(Side.LONG, False)
            )
            if outcome == "missing_child":
                child = next(
                    k for k, o in exchange.orders.items() if o.get("parentOrderLinkId")
                )
                del exchange.orders[child]
            exchanges.append(exchange)
            return stack.enter_context(executor_for(exchange))

        monkeypatch.setattr(demo.asyncio, "create_subprocess_exec", worker)
        monkeypatch.setattr(demo, "executor", recovery_executor)
        if outcome == "timeout":
            monkeypatch.setattr(
                demo.asyncio,
                "wait_for",
                lambda task, timeout: wait_for(task, timeout=0.05),
            )
        if outcome == "clean":
            asyncio.run(demo.observe_zone_process_recovery(Side.LONG))
        else:
            with pytest.raises(
                TimeoutError if outcome == "timeout" else AssertionError
            ):
                asyncio.run(demo.observe_zone_process_recovery(Side.LONG))
    assert len(workers) == len(exchanges) == 1
    assert workers[0].returncode is not None
    assert exchanges[0].size == 0
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    observations = [
        r for r in records if r["check"] == "zone_process_recovery_observation"
    ]
    assert bool(observations) is (outcome == "clean")
    assert records[-1]["check"] == "cleanup" and records[-1]["flat"] is True
    creates = [
        body for path, body in exchanges[0].requests if path == "/v5/order/create"
    ]
    assert len(creates) == int(outcome != "timeout")
    assert all(body["reduceOnly"] for body in creates)


@pytest.mark.parametrize(
    "change",
    [
        {"symbol": "BTCUSDT"},
        {"started_ms": 0},
        {"submitted_links": ["duplicate", "duplicate"]},
        {"cleanup_close_submitted": "false"},
    ],
)
def test_malformed_journal_is_refused_before_exchange_reads(tmp_path, change):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    journal = tmp_path / "owner.json"
    journal.write_text(
        json.dumps(
            {
                "version": 1,
                "symbol": demo.SYMBOL,
                "started_ms": int(datetime.now(UTC).timestamp() * 1000),
                "submitted_links": [],
                "order_ids": [],
                "cleanup_close_submitted": False,
                "observations": {},
                **change,
            }
        )
    )
    exchange = SimpleNamespace(account_state=AsyncMock())
    with pytest.raises(AssertionError):
        asyncio.run(
            demo.ProbeOwnership.recover(
                exchange, journal, Side.LONG, started_after_ms=1
            )
        )
    exchange.account_state.assert_not_called()
