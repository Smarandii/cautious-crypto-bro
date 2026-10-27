"""Safe read retries through real signing/HTTP and supervisor boundaries."""

import asyncio
import itertools

import httpx
import pytest
from test_remaining_concerns import Exchange, executor_for, prepare
from test_supervision_health import stop, until

from cautious_crypto_bro.bybit import BybitClient
from cautious_crypto_bro.domain import ApprovalMode, IntentStatus, TradeExecutionError

pytestmark = pytest.mark.integration


@pytest.mark.parametrize(
    "method,outcomes,expected_calls,succeeds",
    [
        ("GET", ["timeout", "ok"], 2, True),
        ("GET", ["connect_timeout", "ok"], 2, True),
        ("GET", ["timeout", "timeout"], 2, False),
        ("GET", [10010], 1, False),
        ("GET", [10002, "ok"], 2, True),
        ("GET", ["timeout", 10002], 2, False),
        ("POST", ["timeout", "ok"], 1, False),
        ("POST", [10002, "ok"], 2, True),
        ("PUBLIC", ["timeout", "ok"], 1, False),
    ],
)
def test_private_get_retry_budget_never_replays_uncertain_post(
    monkeypatch, method, outcomes, expected_calls, succeeds
):
    clock = itertools.count(100_000)
    monkeypatch.setattr(
        "cautious_crypto_bro.bybit.auth._wall_clock_ms", lambda: next(clock)
    )
    calls, accepted = [], []

    def handle(request):
        if request.url.path == "/v5/market/time":
            return httpx.Response(
                200, json={"retCode": 0, "time": 100_000, "result": {}}
            )
        calls.append(request)
        outcome = outcomes[len(calls) - 1]
        if request.method == "POST" and outcome in {"timeout", "ok"}:
            accepted.append(request.content)
        if outcome in {"timeout", "connect_timeout"}:
            error = httpx.ReadTimeout if outcome == "timeout" else httpx.ConnectTimeout
            raise error("synthetic timeout", request=request)
        return httpx.Response(
            200,
            json={
                "retCode": 0 if outcome == "ok" else outcome,
                "retMsg": "synthetic",
                "result": {"verified": True},
            },
        )

    client = BybitClient(
        base_url="https://api-demo.bybit.com",
        api_key="synthetic-key",
        api_secret="synthetic-secret",
    )
    client._http_client.close()
    client._http_client = httpx.Client(
        base_url="https://api-demo.bybit.com",
        transport=httpx.MockTransport(handle),
        timeout=10,
    )
    try:

        def call():
            if method == "PUBLIC":
                return client.public_get("/v5/market/tickers")
            if method == "POST":
                return client.private_post(
                    "/v5/order/create", {"orderLinkId": "one-submission"}
                )
            return client.private_get("/v5/order/history", {"orderLinkId": "owned"})

        if succeeds:
            assert call()["result"]["verified"] is True
        else:
            with pytest.raises(TradeExecutionError):
                call()
        assert len(calls) == expected_calls
        if method != "PUBLIC":
            assert all(r.headers.get("X-BAPI-SIGN") for r in calls)
            if len(calls) == 2 and outcomes[0] in {"timeout", "connect_timeout"}:
                assert (
                    calls[0].headers["X-BAPI-TIMESTAMP"]
                    != calls[1].headers["X-BAPI-TIMESTAMP"]
                )
        assert len(accepted) == int(method == "POST")
    finally:
        client.close()


def test_single_read_timeout_does_not_escalate_healthy_supervision(tmp_path):
    class TransientExchange(Exchange):
        armed = False
        read_attempts = 0
        failures = 0

        def handle(self, request):
            if self.armed and request.url.path == "/v5/position/list":
                self.read_attempts += 1
                if not self.failures:
                    self.failures += 1
                    raise httpx.ReadTimeout(
                        "synthetic one-off timeout", request=request
                    )
            return super().handle(request)

    async def run():
        exchange = TransientExchange()
        with executor_for(exchange) as executor:
            _, intent, _, coordinator, supervisor = await prepare(
                tmp_path / "state.db", exchange, executor
            )
            result = await coordinator.execute_intent(
                intent.intent_id, approval_mode=ApprovalMode.MANUAL
            )
            assert result.status is IntentStatus.EXECUTED
            await supervisor.reconcile_once()
            baseline_stop = exchange.stop
            reports = []

            async def report(reason):
                reports.append(reason)

            supervisor._report_state = report
            supervisor._progress_path = tmp_path / "progress.json"
            exchange.armed = True
            task = asyncio.create_task(supervisor.run())
            try:
                await until(supervisor._progress_path.exists)
                assert exchange.failures == 1 and exchange.read_attempts >= 2
                assert reports and all(reason is None for reason in reports)
                assert exchange.stop == baseline_stop and baseline_stop > 0
            finally:
                await stop(task)

    asyncio.run(run())
