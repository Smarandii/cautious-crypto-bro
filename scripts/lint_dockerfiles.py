"""Stream selected Dockerfiles to the linter without sharing the workspace."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("files", type=Path, nargs="+")
    args = parser.parse_args(argv)
    for source in args.files:
        print(f"Linting {source}", flush=True)
        result = subprocess.run(
            [
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
            ],
            input=source.read_bytes(),
            check=False,
        )
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
