"""Run isolated Bybit Demo E2E tests and resume the normal app after cleanup."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path


def cleanup_verified(result_path: Path) -> bool:
    """Only a matching cleanup receipt clears each started trading case."""
    pending = set()
    for line in result_path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("{"):
            continue
        try:
            record = json.loads(line)
        except ValueError:
            return False
        if not isinstance(record, dict):
            return False
        if record.get("check") == "mutation_started":
            side = record.get("side")
            if side not in {"LONG", "SHORT"}:
                return False
            pending.add(side)
        elif (
            record.get("check") == "cleanup"
            and record.get("symbol") == "DOGEUSDT"
            and record.get("flat") is True
        ):
            pending.discard(record.get("side"))
    return not pending


def run_suite(repo: Path) -> int:
    def command(*args: str) -> str:
        return subprocess.run(
            args,
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.strip()

    def digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    evidence = repo / "audit-artifacts" / f"bybit-demo-{stamp}"
    evidence.mkdir(parents=True)
    results = evidence / "results.jsonl"
    paused = False
    safe_to_resume = True
    container = ""
    exit_code = 1
    try:
        container = command("docker", "compose", "ps", "-q", "app")
        if not container or len(container.splitlines()) != 1:
            raise RuntimeError("Expected one running normal app container")
        state = json.loads(
            command("docker", "inspect", container, "--format", "{{json .State}}")
        )
        if not state["Running"] or state["Paused"]:
            raise RuntimeError("App must be running and unpaused before testing")
        manifest = {
            "started_utc": datetime.now(UTC).isoformat(),
            "revision": command("git", "rev-parse", "HEAD"),
            "tracked_changes": command("git", "diff", "--name-only").splitlines(),
            "source_sha256": {
                p.relative_to(repo).as_posix(): digest(p)
                for p in sorted((repo / "src").rglob("*.py"))
            },
            "suite_sha256": digest(repo / "scripts" / "e2e_bybit_demo.py"),
            "runner_sha256": digest(Path(__file__)),
            "normal_container": container,
            "normal_started_at": state["StartedAt"],
        }
        (evidence / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n",
            encoding="utf-8",
        )
        command("docker", "pause", container)
        paused = True
        with results.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [
                    "docker",
                    "compose",
                    "run",
                    "--rm",
                    "-T",
                    "--no-deps",
                    "--entrypoint",
                    "/app/.venv/bin/python",
                    "app",
                    "/app/scripts/e2e_bybit_demo.py",
                    "--execute-demo",
                ],
                cwd=repo,
                stdout=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            safe_to_resume = False
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    log.write(line)
                    log.flush()
                    print(line, end="", flush=True)
                exit_code = process.wait()
            except BaseException:
                # Stopping the Docker client does not prove its container stopped.
                # Leave the app paused if this run's completion cannot be verified.
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
            finally:
                if process.stdout is not None:
                    process.stdout.close()
        safe_to_resume = cleanup_verified(results)
        if not safe_to_resume:
            exit_code = 1
        if exit_code == 0:
            records = [
                json.loads(line)
                for line in results.read_text(encoding="utf-8").splitlines()
                if line.startswith("{")
            ]
            if not any(
                r.get("check") == "suite" and r.get("result") == "passed"
                for r in records
            ):
                raise RuntimeError("Suite exited without a passing result")
    except (Exception, KeyboardInterrupt) as exc:
        exit_code = 1
        print(f"Bybit Demo E2E failed: {exc}", file=sys.stderr)
    finally:
        if paused:
            if safe_to_resume:
                try:
                    command("docker", "unpause", container)
                    print("Normal app resumed without restart.")
                except Exception as exc:
                    exit_code = 1
                    print(f"Could not unpause {container}: {exc}", file=sys.stderr)
            else:
                print(
                    f"Cleanup/completion unverified. App remains paused: {container}. "
                    f"Inspect {results} and the test container before unpausing.",
                    file=sys.stderr,
                )
        print(f"Evidence: {evidence}")
    return exit_code


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute-demo",
        action="store_true",
        help="Place small real Bybit Demo test orders",
    )
    args = parser.parse_args()
    if not args.execute_demo:
        parser.error("Requires --execute-demo; this suite places Bybit Demo orders")
    return run_suite(Path(__file__).resolve().parents[1])


if __name__ == "__main__":
    raise SystemExit(main())
