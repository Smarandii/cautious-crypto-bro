"""Run isolated Bybit Demo E2E tests and resume the normal app after cleanup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
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


def pair_observations_verified(records: list[dict]) -> bool:
    seen = set()
    for record in records:
        if record.get("check") != "paired_zone_cancellation_observation":
            continue
        side = record.get("side")
        if side not in {"LONG", "SHORT"} or side in seen:
            return False
        valid = (
            record.get("b_original_pair_preserved") is True
            and record.get("activation_proven") is False
            and isinstance(record.get("cancellation_acknowledged"), bool)
            and record.get("a_tp_terminal") in {"Cancelled", "Deactivated"}
            and record.get("a_peer_sl_status")
            in {"Untriggered", "Cancelled", "Deactivated"}
            and isinstance(record.get("a_peer_retired"), bool)
            and record["a_peer_retired"]
            == (record["a_peer_sl_status"] != "Untriggered")
        )
        if not valid:
            return False
        seen.add(side)
    return seen == {"LONG", "SHORT"}


def recovery_observations_verified(records: list[dict]) -> bool:
    observed = [
        r for r in records if r.get("check") == "zone_process_recovery_observation"
    ]
    return (
        len(observed) == 2
        and {r.get("side") for r in observed} == {"LONG", "SHORT"}
        and all(
            r.get("worker_exit_code") == 86
            and r.get("original_pairs_preserved") is True
            and r.get("recovered_from_journal") is True
            and r.get("entry_submission_replayed") is False
            and r.get("activation_proven") is False
            for r in observed
        )
    )


def amendment_observations_verified(records: list[dict]) -> bool:
    observed = [r for r in records if r.get("check") == "zone_amendment_observation"]
    return (
        len(observed) == 2
        and {r.get("side") for r in observed} == {"LONG", "SHORT"}
        and all(
            r.get("acknowledged") is True
            and r.get("a_sl_identity_preserved") is True
            and r.get("a_sl_trigger_matches_request") is True
            and r.get("b_pair_preserved") is True
            and isinstance(r.get("a_tp_still_present"), bool)
            and r.get("a_tp_trigger_filled") is True
            and r.get("a_sl_peer_cancelled") is True
            and r.get("b_remainder_units") == "2"
            and r.get("activation_proven") is False
            for r in observed
        )
    )


def run_suite(
    repo: Path,
    *,
    zone_protection: bool = False,
    zone_pair_cancellation: bool = False,
    zone_process_recovery: bool = False,
    zone_amendment: bool = False,
) -> int:
    if zone_process_recovery and (zone_protection or zone_pair_cancellation):
        raise ValueError("Choose a single zone observation mode")
    if zone_amendment and (
        zone_protection or zone_pair_cancellation or zone_process_recovery
    ):
        raise ValueError("Choose a single zone observation mode")
    zone_protection = zone_protection or zone_pair_cancellation

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
        image = command("docker", "inspect", container, "--format", "{{.Image}}")
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
            "runtime_image": image,
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
                    "--pull",
                    "never",
                    "--rm",
                    "-T",
                    "--no-deps",
                    "--volume",
                    f"{repo / 'scripts' / 'e2e_bybit_demo.py'}:/app/scripts/e2e_bybit_demo.py:ro",
                    "--volume",
                    f"{evidence}:/probe-evidence",
                    "--env",
                    "CCB_PROBE_EVIDENCE_DIR=/probe-evidence",
                    "--entrypoint",
                    "/app/.venv/bin/python",
                    "app",
                    "/app/scripts/e2e_bybit_demo.py",
                    "--execute-demo",
                    *(["--zone-protection"] if zone_protection else []),
                    *(["--zone-pair-cancellation"] if zone_pair_cancellation else []),
                    *(["--zone-process-recovery"] if zone_process_recovery else []),
                    *(["--zone-amendment"] if zone_amendment else []),
                ],
                cwd=repo,
                env=os.environ | {"CCB_IMAGE": image},
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
            if zone_pair_cancellation and not pair_observations_verified(records):
                raise RuntimeError("Paired cancellation observations are incomplete")
            if zone_process_recovery and not recovery_observations_verified(records):
                raise RuntimeError("Process recovery observations are incomplete")
            if zone_amendment and not amendment_observations_verified(records):
                raise RuntimeError("Amendment observations are incomplete")
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
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--zone-protection",
        action="store_true",
        help="Observe parent-linked partial stops after an independent exit",
    )
    modes.add_argument(
        "--zone-pair-cancellation",
        action="store_true",
        help="Observe exact paired-child cancellation after zone A closes",
    )
    modes.add_argument(
        "--zone-process-recovery",
        action="store_true",
        help="Observe native protection after abrupt worker exit and journal recovery",
    )
    modes.add_argument(
        "--zone-amendment",
        action="store_true",
        help="Observe one-sided partial-stop amendment against the untouched zone",
    )
    args = parser.parse_args()
    if not args.execute_demo:
        parser.error("Requires --execute-demo; this suite places Bybit Demo orders")
    return run_suite(
        Path(__file__).resolve().parents[1],
        zone_protection=args.zone_protection,
        zone_pair_cancellation=args.zone_pair_cancellation,
        zone_process_recovery=args.zone_process_recovery,
        zone_amendment=args.zone_amendment,
    )


if __name__ == "__main__":
    raise SystemExit(main())
