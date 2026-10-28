"""Exercise Docker orchestration without Docker, credentials or exchange calls."""

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "demo_runner", Path(__file__).parents[1] / "scripts" / "run_bybit_demo_e2e.py"
)
assert spec is not None and spec.loader is not None
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)

START = {"check": "mutation_started", "side": "LONG"}
CLEAN = {"check": "cleanup", "side": "LONG", "symbol": "DOGEUSDT", "flat": True}
PASS = {"check": "suite", "result": "passed"}

PAIR = {
    "check": "paired_zone_cancellation_observation",
    "side": "LONG",
    "b_original_pair_preserved": True,
    "activation_proven": False,
    "cancellation_acknowledged": True,
    "a_tp_terminal": "Cancelled",
    "a_peer_sl_status": "Cancelled",
    "a_peer_retired": True,
}

AMENDMENT = {
    "check": "zone_amendment_observation",
    "side": "LONG",
    "acknowledged": True,
    "a_sl_identity_preserved": True,
    "a_sl_trigger_matches_request": True,
    "a_tp_still_present": True,
    "b_pair_preserved": True,
    "activation_proven": False,
}
AMENDED_PAIR = {
    **PAIR,
    "check": "zone_amendment_pair_cancellation_observation",
    "amendment_verified": True,
    "net_quantity_unchanged": True,
    "settling_seconds": 2,
}

RECOVERY = {
    "check": "zone_process_recovery_observation",
    "side": "LONG",
    "worker_exit_code": 86,
    "original_pairs_preserved": True,
    "recovered_from_journal": True,
    "entry_submission_replayed": False,
    "activation_proven": False,
}


@pytest.mark.parametrize(
    "change",
    [
        None,
        {"worker_exit_code": 0},
        {"entry_submission_replayed": True},
        {"original_pairs_preserved": False},
    ],
)
def test_process_recovery_requires_confirmed_worker_exit_and_both_sides(change):
    records = [RECOVERY, {**RECOVERY, "side": "SHORT", **(change or {})}]
    assert runner.recovery_observations_verified(records) is (change is None)
    assert not runner.recovery_observations_verified(records[:1])
    assert not runner.recovery_observations_verified(records + records)


@pytest.mark.parametrize(
    "change",
    [
        None,
        {"a_tp_terminal": "Filled"},
        {"b_original_pair_preserved": False},
        {"a_peer_sl_status": "Unknown"},
        {"a_peer_retired": False},
        {"cancellation_acknowledged": None},
    ],
)
def test_pair_receipts_require_terminal_proof_for_both_sides(change):
    records = [PAIR, {**PAIR, "side": "SHORT"}]
    if change is not None:
        records[1].update(change)
    assert runner.pair_observations_verified(records) is (change is None)
    assert not runner.pair_observations_verified(records[:1])
    assert not runner.pair_observations_verified(records + records)


def test_lost_acknowledgment_can_pass_only_with_terminal_receipts():
    records = [
        {**PAIR, "cancellation_acknowledged": False},
        {
            **PAIR,
            "side": "SHORT",
            "a_peer_sl_status": "Untriggered",
            "a_peer_retired": False,
        },
    ]
    assert runner.pair_observations_verified(records)


@pytest.mark.parametrize(
    "records,expected",
    [
        ([START, {**CLEAN, "side": "SHORT"}], False),
        ([START, {**CLEAN, "symbol": "LINKUSDT"}], False),
        ([START, {**CLEAN, "flat": False}], False),
        ([START, CLEAN, {**START, "side": "SHORT"}], False),
    ],
)
def test_cleanup_requires_matching_receipts(tmp_path, records, expected):
    log = tmp_path / "results.jsonl"
    log.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    assert runner.cleanup_verified(log) is expected


def test_opt_in_precedes_docker(monkeypatch):
    monkeypatch.setattr(runner.sys, "argv", ["runner"])

    def forbidden(_):
        pytest.fail("Runner must not start without opt-in")

    monkeypatch.setattr(runner, "run_suite", forbidden)
    with pytest.raises(SystemExit) as exc:
        runner.main()
    assert exc.value.code == 2


def test_conflicting_zone_modes_refused_before_docker(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Conflicting modes must not start Docker")

    monkeypatch.setattr(runner.subprocess, "run", forbidden)
    with pytest.raises(ValueError, match="single zone observation mode"):
        runner.run_suite(
            tmp_path, zone_pair_cancellation=True, zone_process_recovery=True
        )


@pytest.mark.parametrize(
    "records,process_exit,expected_exit,resumes",
    [
        ([START, CLEAN, PASS], 0, 0, True),
        ([START, CLEAN], 1, 1, True),
        ([START], 1, 1, False),
        ([{"check": "suite_error"}], 1, 1, True),
        ([], 0, 1, True),
    ],
)
@pytest.mark.parametrize(
    "mode",
    [
        "standard",
        "zones",
        "pairs",
        "pairs-missing",
        "recovery",
        "recovery-missing",
        "amended-pair",
        "amended-pair-missing",
    ],
)
def test_runner_lifecycle(
    tmp_path,
    monkeypatch,
    records,
    process_exit,
    expected_exit,
    resumes,
    mode,
):
    zone_protection = mode == "zones"
    paired = mode.startswith("pairs")
    recovery = mode.startswith("recovery")
    amended_pair = mode.startswith("amended-pair")
    if amended_pair and process_exit == 0 and expected_exit == 0:
        records = [*records, AMENDMENT, {**AMENDMENT, "side": "SHORT"}]
        if mode == "amended-pair":
            records = [*records, AMENDED_PAIR, {**AMENDED_PAIR, "side": "SHORT"}]
        else:
            expected_exit = 1
    if recovery and process_exit == 0 and expected_exit == 0:
        if mode == "recovery":
            records = [*records, RECOVERY, {**RECOVERY, "side": "SHORT"}]
        else:
            expected_exit = 1
    if paired and process_exit == 0 and expected_exit == 0:
        if mode == "pairs":
            records = [*records, PAIR, {**PAIR, "side": "SHORT"}]
        else:
            expected_exit = 1
    image = "sha256:" + "a" * 64
    monkeypatch.setenv("CCB_IMAGE", "stale-image:local")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "e2e_bybit_demo.py").write_text(
        "# fixture", encoding="utf-8"
    )
    (tmp_path / "src").mkdir()
    calls = []

    def command(args, **kwargs):
        calls.append(tuple(args))
        assert kwargs["cwd"] == tmp_path
        if args[:3] == ("docker", "compose", "ps"):
            output = "app-container"
        elif args[:2] == ("docker", "inspect"):
            output = (
                image
                if args[-1] == "{{.Image}}"
                else json.dumps(
                    {"Running": True, "Paused": False, "StartedAt": "original"}
                )
            )
        elif args[:2] == ("git", "rev-parse"):
            output = "commit"
        else:
            output = ""
        return SimpleNamespace(stdout=output)

    def process(args, **kwargs):
        calls.append(tuple(args))
        assert kwargs.get("env", {}).get("CCB_IMAGE") == image, (
            "E2E must run the deployed image, regardless of the default Compose tag"
        )
        assert args[args.index("--pull") + 1] == "never"
        assert args[args.index("--volume") + 1] == (
            f"{tmp_path / 'scripts' / 'e2e_bybit_demo.py'}:/app/scripts/e2e_bybit_demo.py:ro"
        )
        assert ("--zone-protection" in args) is (zone_protection or paired)
        assert ("--zone-pair-cancellation" in args) is paired
        assert ("--zone-process-recovery" in args) is recovery
        assert ("--zone-amendment-pair-cancellation" in args) is amended_pair
        assert "--zone-amendment" not in args
        return SimpleNamespace(
            stdout=io.StringIO("\n".join(json.dumps(r) for r in records)),
            wait=lambda: process_exit,
        )

    monkeypatch.setattr(runner.subprocess, "run", command)
    monkeypatch.setattr(runner.subprocess, "Popen", process)
    assert (
        runner.run_suite(
            tmp_path,
            zone_protection=zone_protection,
            zone_pair_cancellation=paired,
            zone_process_recovery=recovery,
            zone_amendment_pair_cancellation=amended_pair,
        )
        == expected_exit
    )
    assert ("docker", "pause", "app-container") in calls
    assert (("docker", "unpause", "app-container") in calls) is resumes
    assert not any("restart" in call for call in calls)
    evidence = next((tmp_path / "audit-artifacts").iterdir())
    manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["normal_started_at"] == "original"
    assert manifest["runtime_image"] == image
    assert len(manifest["suite_sha256"]) == 64
    assert len(manifest["runner_sha256"]) == 64


@pytest.mark.parametrize(
    "change",
    [
        None,
        {"amendment_verified": False},
        {"net_quantity_unchanged": False},
        {"settling_seconds": 0},
        {"a_tp_terminal": "Unknown"},
        {"a_peer_retired": False},
    ],
)
def test_amended_pair_requires_distinct_complete_both_side_receipts(change):
    records = [AMENDED_PAIR, {**AMENDED_PAIR, "side": "SHORT", **(change or {})}]
    assert runner.amendment_pair_observations_verified(records) is (change is None)
    assert not runner.amendment_pair_observations_verified(records[:1])
    assert not runner.amendment_pair_observations_verified(records + records)
    assert not runner.amendment_pair_observations_verified(
        [PAIR, {**PAIR, "side": "SHORT"}]
    )


def test_amended_pair_accepts_observed_unbound_peer_with_lost_ack():
    records = [
        {
            **AMENDED_PAIR,
            "side": side,
            "a_peer_retired": False,
            "a_peer_sl_status": "Untriggered",
            "cancellation_acknowledged": False,
        }
        for side in ("LONG", "SHORT")
    ]
    assert runner.amendment_pair_observations_verified(records)


@pytest.mark.parametrize(
    "other",
    [
        "zone_protection",
        "zone_pair_cancellation",
        "zone_process_recovery",
        "zone_amendment",
    ],
)
def test_amended_pair_refuses_conflicting_mode_before_docker(
    tmp_path, monkeypatch, other
):
    def forbidden(*args, **kwargs):
        pytest.fail("Conflicting modes reached Docker")

    monkeypatch.setattr(runner.subprocess, "run", forbidden)
    with pytest.raises(ValueError, match="single zone observation mode"):
        runner.run_suite(
            tmp_path, zone_amendment_pair_cancellation=True, **{other: True}
        )
