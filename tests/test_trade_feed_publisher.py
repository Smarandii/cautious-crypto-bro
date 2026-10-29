"""Background command creation is explicit without changing command safeguards."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import publish_trade_feed as publisher  # noqa: E402


@pytest.mark.parametrize(
    "platform,expected_flags", [("win32", 0x08000000), ("linux", 0)]
)
def test_background_commands_keep_capture_and_hide_windows(
    monkeypatch, platform, expected_flags
):
    observed = {}

    def run(command, **kwargs):
        observed.update(command=command, **kwargs)
        return SimpleNamespace(returncode=0, stdout="public result")

    monkeypatch.setattr(publisher, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(
        publisher,
        "subprocess",
        SimpleNamespace(run=run, CREATE_NO_WINDOW=0x08000000),
    )
    assert (
        publisher._run(["git", "credential", "fill"], input_text="input", timeout=12)
        == "public result"
    )
    assert observed["creationflags"] == expected_flags
    assert observed["command"] == ["git", "credential", "fill"]
    assert observed["input"] == "input"
    assert observed["timeout"] == 12
    assert observed["capture_output"] and observed["text"]
    assert observed["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert observed["env"]["GCM_INTERACTIVE"] == "never"


def test_failed_background_command_keeps_private_output_out_of_error(monkeypatch):
    monkeypatch.setattr(
        publisher.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout="PRIVATE_CANARY", stderr="PRIVATE_CANARY"
        ),
    )
    with pytest.raises(RuntimeError) as error:
        publisher._run(["unused"])
    assert "PRIVATE_CANARY" not in str(error.value)


def test_background_command_timeout_is_preserved(monkeypatch):
    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(publisher.subprocess, "run", timed_out)
    with pytest.raises(subprocess.TimeoutExpired) as error:
        publisher._run(["unused"], timeout=12)
    assert error.value.timeout == 12
