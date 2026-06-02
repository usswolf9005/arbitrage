from __future__ import annotations

from collections.abc import Mapping
from typing import Any


TERMINAL_EXECUTION_STATUSES = {"SETTLED", "FAILED", "ABORTED", "MANUAL_REVIEW", "BLOCKED"}


def idempotency_scope_conflict(
    run: Mapping[str, Any],
    *,
    opportunity_id: int,
    route_id: int,
    mode: str,
    trade_amount_krw: float | None = None,
) -> bool:
    """Return True when an idempotency key belongs to a different execution scope."""
    if int(run.get("opportunity_id") or 0) != int(opportunity_id):
        return True
    if int(run.get("route_id") or 0) != int(route_id):
        return True
    if str(run.get("mode") or "") != str(mode or ""):
        return True

    requested_amount = _optional_float(trade_amount_krw)
    stored_amount = _payload_amount(run.get("payload"))
    if requested_amount is None or stored_amount is None:
        return False
    return abs(float(requested_amount) - float(stored_amount)) > 0.000001


def idempotency_conflict_response(run: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "existing": True,
        "run": dict(run),
        "error_code": "idempotency_scope_conflict",
    }


def existing_run_response(run: Mapping[str, Any], *, non_ok_statuses: set[str]) -> dict[str, Any]:
    return {
        "ok": str(run.get("status") or "") not in non_ok_statuses,
        "existing": True,
        "run": dict(run),
    }


def _payload_amount(payload: Any) -> float | None:
    if not isinstance(payload, Mapping):
        return None
    for key in ("trade_amount_krw", "amount_krw", "approved_amount_krw"):
        if key not in payload:
            continue
        return _optional_float(payload.get(key))
    return None


def _optional_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
