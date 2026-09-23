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


@pytest.mark.parametrize(
    "records,expected",
    [
        ([START, CLEAN], True),
        ([START], False),
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
def test_runner_lifecycle(
    tmp_path, monkeypatch, records, process_exit, expected_exit, resumes
):
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
            output = json.dumps(
                {"Running": True, "Paused": False, "StartedAt": "original"}
            )
        elif args[:2] == ("git", "rev-parse"):
            output = "commit"
        else:
            output = ""
        return SimpleNamespace(stdout=output)

    def process(args, **kwargs):
        calls.append(tuple(args))
        return SimpleNamespace(
            stdout=io.StringIO("\n".join(json.dumps(r) for r in records)),
            wait=lambda: process_exit,
        )

    monkeypatch.setattr(runner.subprocess, "run", command)
    monkeypatch.setattr(runner.subprocess, "Popen", process)
    assert runner.run_suite(tmp_path) == expected_exit
    assert ("docker", "pause", "app-container") in calls
    assert (("docker", "unpause", "app-container") in calls) is resumes
    assert not any("restart" in call for call in calls)
    evidence = next((tmp_path / "audit-artifacts").iterdir())
    manifest = json.loads((evidence / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["normal_started_at"] == "original"
    assert len(manifest["suite_sha256"]) == 64
    assert len(manifest["runner_sha256"]) == 64
