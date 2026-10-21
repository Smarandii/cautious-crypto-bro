"""Check the filesystem-to-container boundary without requiring a Docker daemon."""

import runpy
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

lint = runpy.run_path(str(Path(__file__).parents[1] / "scripts/lint_dockerfiles.py"))


@pytest.mark.parametrize("exit_code", [0, 17])
def test_lint_streams_only_selected_files_and_propagates_failure(
    tmp_path, monkeypatch, exit_code
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_bytes(b"synthetic private canary")
    (tmp_path / "nested path").mkdir()
    files = [Path("Dockerfile"), Path("nested path/Service.Dockerfile")]
    contents = [b"FROM scratch\n", b"FROM scratch\n# second Dockerfile\n"]
    for path, content in zip(files, contents, strict=True):
        path.write_bytes(content)
    received = []

    def docker(args, *, input, check):
        # An argument allowlist prevents workspace mounts, env forwarding, shell
        # execution and privileged flags from silently entering this boundary.
        assert args == [
            "docker",
            "run",
            "--rm",
            "-i",
            "--network",
            "none",
            "ghcr.io/immanuwell/droast:1.7.0",
            "--preset",
            "production",
            "--fail-on",
            "warning",
            "--no-roast",
            "-",
        ]
        assert not check
        assert input == contents[len(received)]
        assert b"private canary" not in input
        received.append(input)
        return SimpleNamespace(returncode=exit_code)

    monkeypatch.setattr(subprocess, "run", docker)
    assert lint["main"]([str(path) for path in files]) == exit_code
    assert received == (contents if exit_code == 0 else contents[:1])
