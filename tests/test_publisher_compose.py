"""Validate the rendered service boundary with Compose, without starting containers."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.integration
@pytest.mark.skipif(
    os.name == "nt" or shutil.which("docker") is None,
    reason="Linux Compose CLI integration; Windows verified through WSL",
)
def test_publisher_compose_has_read_only_inputs_without_host_control(tmp_path):
    (tmp_path / ".env").write_bytes((ROOT / ".env.example").read_bytes())
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            "publisher-contract",
            "--project-directory",
            str(tmp_path),
            "-f",
            str(ROOT / "docker-compose.yaml"),
            "--profile",
            "publisher",
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    services = json.loads(result.stdout)["services"]
    publisher = services["publisher"]
    assert publisher["profiles"] == ["publisher"]
    assert publisher["restart"] == "unless-stopped" and publisher["read_only"]
    assert not publisher.get("ports") and not publisher.get("privileged")
    assert publisher["cap_drop"] == ["ALL"]
    mounts = {mount["target"]: mount for mount in publisher["volumes"]}
    assert set(mounts) == {"/state", "/run/secrets", "/work"}
    assert mounts["/state"]["read_only"] and mounts["/run/secrets"]["read_only"]
    assert not mounts["/work"].get("read_only", False)
    assert mounts["/work"]["source"] != mounts["/state"]["source"]
    assert all(
        key.startswith("DEMO_")
        or key in {"DATABASE_PATH", "PUBLISHER_WORK_DIR", "PUBLISHER_SECRETS_DIR"}
        for key in publisher["environment"]
    )
    assert all(
        services["app"]["environment"][key] == value
        for key, value in publisher["environment"].items()
        if key.startswith("DEMO_")
    )
    assert publisher["healthcheck"]["test"][-1] == "--health"
