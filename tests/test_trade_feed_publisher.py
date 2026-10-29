"""Container publication preserves privacy, bounded execution and observable recovery."""

import io
import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

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

    def export(config, bundle, output):
        assert config == settings
        bundle.write_text("PRIVATE_ARCHIVE_CANARY", encoding="utf-8")
        output.write_text(json.dumps(public), encoding="utf-8")

    def urlopen(request, *, timeout):
        requests.append(request)
        response = io.BytesIO(json.dumps(public).encode())
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
    }
    dispatched = json.loads(requests[0].data)
    assert json.loads(dispatched["inputs"]["trade_feed"]) == public
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
