"""Container publication preserves privacy, bounded execution and observable recovery."""

import io
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from cautious_crypto_bro.dashboard_balance import decode_balance_feed
from cautious_crypto_bro.dashboard_feed import public_feed_from_snapshot
from cautious_crypto_bro.dashboard_metrics import public_trade_history
from cautious_crypto_bro.state_ownership import state_ownership

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publish_trade_feed as publisher  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    secrets = tmp_path / "secrets"
    work = tmp_path / "work"
    secrets.mkdir()
    work.mkdir()
    for name in ("github_token", "bybit_api_key", "bybit_api_secret"):
        (secrets / name).write_text("PRIVATE_CREDENTIAL_CANARY", encoding="utf-8")
    return publisher.PublisherSettings(
        _env_file=None,
        database_path=tmp_path / "state.sqlite3",
        publisher_secrets_dir=secrets,
        publisher_work_dir=work,
    )


def feed():
    history = public_trade_history([], available=True)
    history["as_of"] = datetime.now(UTC).isoformat()
    return public_feed_from_snapshot(
        {"generated_at": history["as_of"], "trade_history": history, "risk": {}}
    )


@pytest.mark.integration
def test_cycle_dispatches_only_public_feed_and_verifies_publication(
    settings, monkeypatch
):
    public = feed()
    requests = []
    balance = {"schema_version": 1, "refresh_failed": True, "history": None}

    def export(config, bundle, output):
        assert config == settings
        bundle.write_text("PRIVATE_ARCHIVE_CANARY", encoding="utf-8")
        output.write_text(json.dumps(public), encoding="utf-8")
        output.with_suffix(".balance.json").write_text(
            json.dumps(balance), encoding="utf-8"
        )

    def urlopen(request, *, timeout):
        requests.append(request)
        value = balance if "balance.json" in request.full_url else public
        response = io.BytesIO(json.dumps(value).encode())
        response.status = 204 if request.get_method() == "POST" else 200
        assert timeout in (15, 30)
        return response

    monkeypatch.setattr(publisher, "_run_export", export)
    monkeypatch.setattr(publisher, "urlopen", urlopen)
    result = publisher.refresh_once(settings)
    assert result == {
        "published": True,
        "as_of": public["trade_history"]["as_of"],
        "completed_trades": 0,
        "balance_as_of": None,
        "balance_refresh_failed": True,
    }
    dispatched = json.loads(requests[0].data)
    assert json.loads(dispatched["inputs"]["trade_feed"]) == public
    assert decode_balance_feed(dispatched["inputs"]["balance_feed"]) == balance
    assert all("PRIVATE" not in (request.data or b"").decode() for request in requests)
    assert requests[0].get_header("Authorization") == "Bearer PRIVATE_CREDENTIAL_CANARY"
    assert not list(settings.publisher_work_dir.iterdir())


def test_private_input_never_reaches_dispatch_and_temp_files_are_removed(
    settings, monkeypatch
):
    def export(config, bundle, output):
        bundle.write_text("PRIVATE_ARCHIVE_CANARY", encoding="utf-8")
        output.write_text(
            json.dumps(feed() | {"source_text": "PRIVATE_SOURCE_CANARY"}),
            encoding="utf-8",
        )

    def forbidden(*args, **kwargs):
        pytest.fail("Private payload reached HTTP dispatch")

    monkeypatch.setattr(publisher, "_run_export", export)
    monkeypatch.setattr(publisher, "urlopen", forbidden)
    with pytest.raises(ValueError, match="fields"):
        publisher.refresh_once(settings)
    assert not list(settings.publisher_work_dir.iterdir())


def test_export_child_is_local_bounded_and_has_no_credential_arguments(
    settings, monkeypatch
):
    seen = {}

    def run(command, **kwargs):
        seen.update(command=command, **kwargs)
        return SimpleNamespace(
            returncode=1, stderr="PRIVATE_CANARY", stdout="PRIVATE_CANARY"
        )

    monkeypatch.setattr(publisher.subprocess, "run", run)
    with pytest.raises(RuntimeError) as error:
        publisher._run_export(settings, Path("private.zip"), Path("public.json"))
    assert "PRIVATE" not in str(error.value)
    assert seen["command"] == [
        sys.executable,
        str(Path(publisher.__file__).resolve()),
        "--export",
        "private.zip",
        "public.json",
    ]
    assert seen["timeout"] == 270 and seen["capture_output"]
    assert "PRIVATE_CREDENTIAL_CANARY" not in json.dumps(seen)


def test_export_timeout_propagates_to_cycle_failure(settings, monkeypatch):
    def run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(publisher.subprocess, "run", run)
    with pytest.raises(subprocess.TimeoutExpired) as error:
        publisher.refresh_once(settings)
    assert error.value.timeout == 270
    assert not list(settings.publisher_work_dir.iterdir())


@pytest.mark.parametrize(
    "age,published,expected",
    [
        (0, True, True),
        (900, True, True),
        (1801, True, False),
        (-60, True, False),
        (0, False, False),
    ],
)
def test_health_requires_a_recent_verified_publication(
    settings, age, published, expected
):
    publisher._write_receipt(
        settings,
        {
            "checked_at": (datetime.now(UTC) - timedelta(seconds=age)).isoformat(),
            "published": published,
        },
    )
    assert publisher.healthy(settings) is expected


def test_absent_or_corrupt_receipt_is_unhealthy(settings):
    assert not publisher.healthy(settings)
    (settings.publisher_work_dir / "status.json").write_text(
        "not json", encoding="utf-8"
    )
    assert not publisher.healthy(settings)


def test_failed_balance_refresh_is_not_reported_as_healthy(settings):
    publisher._write_receipt(
        settings,
        {
            "checked_at": datetime.now(UTC).isoformat(),
            "published": True,
            "balance_refresh_failed": True,
        },
    )
    assert not publisher.healthy(settings)


def test_cycle_failure_is_private_and_next_interval_recovers(
    settings, monkeypatch, capsys
):
    attempts = []
    sleeps = []

    def refresh(config):
        attempts.append(config)
        if len(attempts) == 1:
            raise RuntimeError("PRIVATE_CREDENTIAL_CANARY")
        return {"published": True, "completed_trades": 0}

    def sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise StopIteration

    monkeypatch.setattr(publisher, "refresh_once", refresh)
    monkeypatch.setattr(publisher.time, "sleep", sleep)
    with pytest.raises(StopIteration):
        publisher.publish_loop(settings, once=False)
    receipts = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [row["published"] for row in receipts] == [False, True]
    assert "PRIVATE" not in json.dumps(receipts)
    assert len(attempts) == 2 and all(890 < seconds <= 900 for seconds in sleeps)
    assert publisher.healthy(settings)


def test_singleton_prevents_parallel_publishing_and_startup_clears_old_health(
    settings, monkeypatch
):
    monkeypatch.setattr(publisher, "PublisherSettings", lambda: settings)
    monkeypatch.setattr(sys, "argv", ["publisher", "--once"])
    called = []

    def refresh(config):
        called.append(config)
        assert not publisher.healthy(config)
        return {"published": True}

    monkeypatch.setattr(publisher, "refresh_once", refresh)
    publisher._write_receipt(
        settings, {"published": True, "checked_at": datetime.now(UTC).isoformat()}
    )
    with state_ownership(settings.publisher_work_dir / "publisher"):
        with pytest.raises(RuntimeError, match="already owned"):
            publisher.main()
    assert not called
    with pytest.raises(SystemExit) as result:
        publisher.main()
    assert result.value.code == 0 and len(called) == 1
    assert publisher.healthy(settings)


@pytest.mark.integration
def test_publisher_keeps_private_terminal_evidence_across_temporary_exports(
    settings, monkeypatch
):
    import export_forensic_replay
    import export_trade_feed

    import cautious_crypto_bro.btc_benchmark as benchmark_module
    import cautious_crypto_bro.demo_balance_cache as balance_cache

    settings.database_path.touch()
    public = feed()
    paths = []
    requests = []
    wallet = {"schema_version": 1, "refresh_failed": True, "history": None}
    benchmark = {"refresh_failed": True, "history": None}
    balance = {**wallet, "schema_version": 2, "benchmark": benchmark}
    benchmark_calls = []

    def capture_benchmark(*args, **kwargs):
        benchmark_calls.append(kwargs)
        return benchmark

    def export(bundle, **options):
        path = options["terminal_evidence_path"]
        assert path == settings.publisher_work_dir / "terminal-entry-evidence.json"
        assert path.parent != bundle.parent
        assert path.exists() is bool(paths)
        paths.append(path)
        path.write_text("PRIVATE_RETAINED_ORDER_CANARY", encoding="utf-8")
        bundle.write_text("PRIVATE_ARCHIVE_CANARY", encoding="utf-8")

    def build(database, bundle, risk):
        with pytest.raises(sqlite3.OperationalError):
            database.execute("CREATE TABLE forbidden_write (value TEXT)")
        return public

    def urlopen(request, *, timeout):
        requests.append(request)
        value = balance if "balance.json" in request.full_url else public
        response = io.BytesIO(json.dumps(value).encode())
        response.status = 204 if request.get_method() == "POST" else 200
        return response

    monkeypatch.setattr(export_forensic_replay, "export_bundle", export)
    monkeypatch.setattr(export_trade_feed, "build_trade_feed", build)
    monkeypatch.setattr(
        balance_cache, "collect_balance_feed", lambda *args, **kwargs: wallet
    )
    monkeypatch.setattr(benchmark_module, "collect_benchmark", capture_benchmark)
    monkeypatch.setattr(publisher, "_run_export", publisher.export_feed)
    monkeypatch.setattr(publisher, "urlopen", urlopen)
    assert publisher.refresh_once(settings)["published"]
    assert publisher.refresh_once(settings)["published"]
    assert len(paths) == 2 and paths[0] == paths[1]
    assert len(benchmark_calls) == 2
    assert not list(settings.publisher_work_dir.glob("trade-feed-*"))
    assert paths[0].read_text() == "PRIVATE_RETAINED_ORDER_CANARY"
    assert all(
        "PRIVATE_RETAINED_ORDER_CANARY" not in (r.data or b"").decode()
        for r in requests
    )


@pytest.mark.parametrize("returncode,pending", [(78, False), (1, True)])
def test_private_evidence_failure_has_an_actionable_redacted_receipt(
    settings, monkeypatch, capsys, returncode, pending
):
    def run(*args, **kwargs):
        if pending:
            (
                settings.publisher_work_dir / "terminal-entry-evidence.json.pending"
            ).touch()
        return SimpleNamespace(
            returncode=returncode, stdout="PRIVATE", stderr="PRIVATE"
        )

    monkeypatch.setattr(publisher.subprocess, "run", run)
    with pytest.raises(SystemExit) as result:
        publisher.publish_loop(settings, once=True)
    assert result.value.code == 1
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["error_type"] == "TerminalEvidenceUnavailable"
    assert receipt["published"] is False and not publisher.healthy(settings)
    assert "PRIVATE" not in json.dumps(receipt)
    assert not list(settings.publisher_work_dir.glob("trade-feed-*"))


def test_export_child_redacts_private_evidence_failure(settings, monkeypatch):
    monkeypatch.setattr(publisher, "PublisherSettings", lambda: settings)
    monkeypatch.setattr(
        sys, "argv", ["publisher", "--export", "private.zip", "public.json"]
    )

    def export(*args):
        raise publisher.TerminalEvidenceUnavailable("PRIVATE_DETAIL_CANARY")

    monkeypatch.setattr(publisher, "export_feed", export)
    with pytest.raises(SystemExit) as result:
        publisher.main()
    assert result.value.code == 78


@pytest.mark.integration
@pytest.mark.parametrize("failed", [False, True])
def test_benchmark_status_is_independent_in_publication_receipt(
    settings, monkeypatch, tmp_path, failed
):
    from test_btc_benchmark_integration import BenchmarkHTTP, capture, retained

    http = BenchmarkHTTP()
    benchmark = capture(tmp_path, http)
    benchmark["refresh_failed"] = failed
    balance = {
        "schema_version": 2,
        "refresh_failed": False,
        "history": retained(http),
        "benchmark": benchmark,
    }
    public = feed()

    def export(config, bundle, output):
        output.write_text(json.dumps(public))
        output.with_suffix(".balance.json").write_text(json.dumps(balance))

    def urlopen(request, *, timeout):
        response = io.BytesIO(
            json.dumps(
                balance if "balance.json" in request.full_url else public
            ).encode()
        )
        response.status = 204 if request.get_method() == "POST" else 200
        return response

    monkeypatch.setattr(publisher, "_run_export", export)
    monkeypatch.setattr(publisher, "urlopen", urlopen)
    result = publisher.refresh_once(settings)
    assert result["published"] and not result["balance_refresh_failed"]
    assert result["benchmark_refresh_failed"] is failed
    assert result["benchmark_as_of"] == benchmark["history"]["as_of"]
