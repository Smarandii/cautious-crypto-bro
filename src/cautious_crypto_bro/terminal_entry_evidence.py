"""Retain observed terminal entry facts; revalidate them with every fresh capture."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

from cautious_crypto_bro.entry_settlement import entry_orders_settled
from cautious_crypto_bro.state_ownership import state_ownership

ORDER_FIELDS = {
    "orderId",
    "orderLinkId",
    "symbol",
    "side",
    "reduceOnly",
    "qty",
    "cumExecQty",
    "orderStatus",
    "updatedTime",
}
REQUEST_FIELDS = {"order_id", "order_link_id", "quantity"}


class TerminalEvidenceUnavailable(RuntimeError):
    """Private evidence cannot safely support a new publication."""


def _sync_directory(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def terminal_evidence_scope(database: Path, endpoint: str, api_key: str) -> str:
    # No credential is persisted. Rotation deliberately requires a new evidence scope.
    value = json.dumps([str(database.resolve()), endpoint, api_key])
    return hashlib.sha256(value.encode()).hexdigest()


def _read(path: Path, scope: str, as_of_ms: int) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (
            set(data) != {"version", "scope", "observations"}
            or type(data["version"]) is not int
            or data["version"] != 1
            or data["scope"] != scope
            or not isinstance(data["observations"], dict)
        ):
            raise ValueError
        for order_id, record in data["observations"].items():
            if (
                set(record) != {"expected", "symbol", "side", "order", "captured_at_ms"}
                or set(record["expected"]) != REQUEST_FIELDS
                or not all(isinstance(v, str) for v in record["expected"].values())
                or record["expected"]["order_id"] != order_id
                or not isinstance(record["symbol"], str)
                or record["side"] not in {"Buy", "Sell"}
                or set(record["order"]) != ORDER_FIELDS
                or record["order"]["orderId"] != order_id
                or record["order"]["reduceOnly"] is not False
                or not all(
                    isinstance(v, str)
                    for k, v in record["order"].items()
                    if k != "reduceOnly"
                )
                or type(record["captured_at_ms"]) is not int
                or not 0
                < int(record["order"]["updatedTime"])
                <= record["captured_at_ms"]
                <= as_of_ms
            ):
                raise ValueError
        return data["observations"]
    except (TypeError, KeyError, ValueError, OverflowError) as exc:
        raise TerminalEvidenceUnavailable(
            "Invalid or mismatched private terminal-entry evidence"
        ) from exc


def retain_terminal_entries(
    path: Path,
    *,
    scope: str,
    requests: list[tuple[dict, str, str]],
    history: list[dict],
    current: list[dict],
    fills: dict[str, list[dict]],
    as_of_ms: int,
) -> tuple[list[dict], list[dict]]:
    """Fresh facts win; a revoked record cannot return when fresh history expires."""
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + ".pending")
    with state_ownership(path):
        if pending.exists():
            raise TerminalEvidenceUnavailable(
                "Interrupted terminal-entry evidence update requires review"
            )
        previous = _read(path, scope, as_of_ms)
        # Leave this marker on failure: an old file must not hide an uncommitted
        # revocation after a failed write or killed exporter. Recovery archives
        # both files and starts empty; merely removing the marker is unsafe.
        descriptor = os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w") as marker:
            marker.write("Terminal-entry evidence update pending\n")
            marker.flush()
            os.fsync(marker.fileno())
        _sync_directory(path.parent)
        by_id: dict[str, list[dict]] = {}
        for order in history:
            by_id.setdefault(str(order.get("orderId") or ""), []).append(order)
        retained = {}
        merged = list(history)
        provenance = []
        for expected, symbol, side in requests:
            expected = {key: expected[key] for key in REQUEST_FIELDS}
            order_id = expected["order_id"]
            fresh = by_id.get(order_id, [])
            saved = previous.get(order_id)
            if fresh:
                records = fresh
                captured_at = as_of_ms
            elif saved and (saved["expected"], saved["symbol"], saved["side"]) == (
                expected,
                symbol,
                side,
            ):
                records = [saved["order"]]
                captured_at = saved["captured_at_ms"]
            else:
                continue
            if not entry_orders_settled(
                [expected], symbol, side, records, current, fills
            ):
                continue
            order = records[0]
            try:
                if not 0 < int(order["updatedTime"]) <= captured_at <= as_of_ms:
                    continue
                record = {key: order[key] for key in ORDER_FIELDS}
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
            retained[order_id] = {
                "expected": expected,
                "symbol": symbol,
                "side": side,
                "order": record,
                "captured_at_ms": captured_at,
            }
            if not fresh:
                merged.append(record)
                provenance.append({"order_id": order_id, "captured_at_ms": captured_at})
        _commit(path, pending, scope, retained)
        return merged, provenance


def _commit(path: Path, pending: Path, scope: str, observations: dict) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=path.name + ".",
            delete=False,
        ) as output:
            temporary = Path(output.name)
            os.chmod(temporary, 0o600)
            json.dump(
                {"version": 1, "scope": scope, "observations": observations},
                output,
                allow_nan=False,
            )
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
        pending.unlink()
        _sync_directory(path.parent)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
