from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from .execution_flow import (
    flow_edge_endpoints,
    route_edge_ids,
    route_node_ids,
    step_edge_id,
    step_node_id,
    ui_state_for_step_status,
    unique_ordered,
)
from .auto_small_execution import AutoSmallSameDexDryRunRunner
from .bridge_submit import BridgeSubmitAdapter
from .cex_trade import CexTradeAdapter
from .demo_seed import seed_demo_sol_opportunity
from .dex_submit import DexSwapSubmitAdapter
from .execution_safety import (
    TERMINAL_EXECUTION_STATUSES,
    existing_run_response,
    idempotency_conflict_response,
    idempotency_scope_conflict,
)
from .live_full_execution import LiveFullBridgeCexRunner
from .paper_execution import PaperExecutionRunner, ROUTE_STEPS
from .store import ArbitrageStore, now_ms

EDGE_GATED_MODES = {"one_click", "auto_small", "live_full"}
NON_OK_EXISTING_RUN_STATUSES = {"ABORTED", "BLOCKED", "FAILED", "MANUAL_REVIEW"}
LIVE_FULL_ROUTE_TYPES = {"direct_cex_sell", "bridge_dex_sell", "bridge_cex_sell"}
CEX_ROUTE_TYPES = {"direct_cex_sell", "bridge_cex_sell"}
BRIDGE_ROUTE_TYPES = {"bridge_dex_sell", "bridge_cex_sell"}
PROVIDER_STATUS_PASS = {"pass", "ok", "open", "available", "enabled", "success", "done", "ready", "verified"}
PROVIDER_STATUS_BLOCK = {"block", "blocked", "disabled", "failed", "rejected", "unavailable", "closed", "error"}
PROVIDER_STATUS_PENDING = {"pending", "checking", "in_progress", "processing"}
PROVIDER_STATUS_UNKNOWN = {"unknown", "unsupported", "unverified"}

FRESHNESS_COMPONENT_BY_SOURCE = {
    "buy_quote": "buy_quote",
    "buy_tick": "buy_quote",
    "sell_quote": "sell_quote_or_orderbook",
    "sell_tick": "sell_quote_or_orderbook",
    "orderbook": "sell_quote_or_orderbook",
    "fx": "fx",
    "rpc_block": "rpc_freshness",
    "rpc_freshness": "rpc_freshness",
    "bridge_quote": "bridge_fee",
    "bridge_fee": "bridge_fee",
    "bridge_status": "deposit_or_bridge_status",
    "bridge_availability": "deposit_or_bridge_status",
    "deposit_status": "deposit_or_bridge_status",
    "cex_deposit": "deposit_or_bridge_status",
    "deposit_or_bridge_status": "deposit_or_bridge_status",
}

REQUIRED_FRESHNESS_BY_ROUTE_TYPE = {
    "same_dex_sell": (
        ("buy_quote", "buy_tick"),
        ("sell_quote", "sell_tick"),
        ("rpc_block", "rpc_freshness"),
    ),
    "direct_cex_sell": (
        ("buy_quote", "buy_tick"),
        ("orderbook", "sell_quote", "sell_tick"),
        ("rpc_block", "rpc_freshness"),
        ("deposit_status", "cex_deposit"),
    ),
    "bridge_dex_sell": (
        ("buy_quote", "buy_tick"),
        ("sell_quote", "sell_tick"),
        ("rpc_block", "rpc_freshness"),
        ("bridge_quote", "bridge_fee"),
        ("bridge_status", "bridge_availability"),
    ),
    "bridge_cex_sell": (
        ("buy_quote", "buy_tick"),
        ("orderbook", "sell_quote", "sell_tick"),
        ("rpc_block", "rpc_freshness"),
        ("bridge_quote", "bridge_fee"),
        ("bridge_status", "bridge_availability"),
        ("deposit_status", "cex_deposit"),
    ),
}

KRW_FX_ROUTE_TYPES = {"direct_cex_sell", "bridge_cex_sell"}


SUPPORTED_PRECHECK_CHECK_NAMES: tuple[str, ...] = (
    "sell_quote",
    "small_sell_simulation",
    "transfer_simulation",
    "tax_blacklist",
    "pool_reserve",
    "cex_deposit",
    "bridge_availability",
    "stale_data",
    "route_edge",
    "wallet_permission",
)

PRECHECK_CHECK_NAME_ALIASES = {
    "cex_deposit_status": "cex_deposit",
    "deposit_status": "cex_deposit",
    "bridge_status": "bridge_availability",
}

PRECHECK_STATUS_RANK = {
    "PASS": 0,
    "WARN": 1,
    "BLOCK": 2,
    "ERROR": 3,
}

UI_STATUS = {
    "DETECTED": "done",
    "PRECHECKING": "active",
    "PRECHECK_PASS": "done",
    "PRECHECK_WARN": "warn",
    "BLOCKED": "blocked",
    "ROUTE_READY": "done",
    "EXEC_READY": "done",
    "ENTERING": "active",
    "POSITION_OPEN": "active",
    "EXITING": "active",
    "SETTLED": "done",
    "FAILED": "failed",
    "ABORTED": "failed",
    "MANUAL_REVIEW": "warn",
    "STALE": "stale",
    "WAIT": "wait",
    "CHECKING": "active",
    "OPEN": "done",
    "WARN": "warn",
    "EXECUTING": "active",
    "DONE": "done",
    "SKIPPED": "skipped",
}


def _normalize_precheck_checks(checks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {}
    for raw_check in checks:
        check = dict(raw_check or {})
        name = _normalize_precheck_check_name(check.get("check_name"))
        status = _normalize_precheck_status(check.get("status"))
        check["check_name"] = name
        check["status"] = status
        if status == "ERROR" and not str(check.get("error_code") or ""):
            check["error_code"] = "invalid_precheck_status"

        existing = by_name.get(name)
        if existing is None or PRECHECK_STATUS_RANK[status] >= PRECHECK_STATUS_RANK[str(existing["status"])]:
            by_name[name] = check
    return list(by_name.values())


def _normalize_precheck_check_name(raw_name: Any) -> str:
    name = str(raw_name or "unknown_check").strip().lower().replace("-", "_")
    return PRECHECK_CHECK_NAME_ALIASES.get(name, name)


def _normalize_precheck_status(raw_status: Any) -> str:
    status = str(raw_status or "ERROR").strip().upper()
    return status if status in PRECHECK_STATUS_RANK else "ERROR"


def _precheck_details(raw_details: Any) -> dict[str, Any]:
    return dict(raw_details) if isinstance(raw_details, Mapping) else {}


def _as_mapping(raw: Any) -> Mapping[str, Any]:
    return raw if isinstance(raw, Mapping) else {}


def _as_list(raw: Any) -> list[Any]:
    return list(raw) if isinstance(raw, list) else []


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _truthy_value(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled", "ack", "pass"}
    return False


def _simulation_evidence_not_executable(route: Mapping[str, Any]) -> bool:
    payload = _as_mapping(route.get("payload"))
    if any(
        _truthy_value(payload.get(key))
        for key in (
            "simulation_only",
            "no_real_funds",
            "no_real_submit",
        )
    ):
        return True
    edge_evaluation = _as_mapping(payload.get("edge_evaluation"))
    for section in ("freshness", "component_evidence"):
        for record in _as_mapping(edge_evaluation.get(section)).values():
            details = _as_mapping(_as_mapping(record).get("details"))
            if str(details.get("source_name") or "") == "no_funds_simulation":
                return True
    return False


def _freshness_source_component(source_key: str) -> str:
    normalized = str(source_key or "").strip().lower()
    return FRESHNESS_COMPONENT_BY_SOURCE.get(normalized, normalized)


def _optional_positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _optional_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _first_payload_float(payload: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key not in payload:
            continue
        try:
            return float(payload.get(key))
        except (TypeError, ValueError):
            return None
    return None


def _first_payload_int(payload: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        if key not in payload:
            continue
        try:
            return int(payload.get(key))
        except (TypeError, ValueError):
            return None
    return None


def _approval_payload(approval: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = approval.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _approval_amount_krw(approval: Mapping[str, Any]) -> float | None:
    return _first_payload_float(_approval_payload(approval), "trade_amount_krw", "amount_krw", "approved_amount_krw")


def _approval_expires_at_ms(approval: Mapping[str, Any]) -> int | None:
    return _first_payload_int(
        _approval_payload(approval),
        "expires_at_ms",
        "approval_expires_at_ms",
        "valid_until_ms",
        "window_expires_at_ms",
    )


def _explicit_false(value: Any) -> bool:
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return int(value) == 0
    if isinstance(value, str):
        return value.strip().lower() in {"0", "false", "no", "unverified"}
    return False


def _iter_payload_status_values(payload: Mapping[str, Any], keys: tuple[str, ...]) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []
    containers = [payload]
    for nested_key in ("precheck", "provider_status", "status"):
        nested = payload.get(nested_key)
        if isinstance(nested, Mapping):
            containers.append(nested)
    for container in containers:
        for key in keys:
            if key in container:
                found.append((key, container.get(key)))
    return found


def _provider_status_text(value: Any) -> str:
    if isinstance(value, Mapping):
        raw = value.get("status", value.get("state", value.get("result")))
    else:
        raw = value
    return str(raw or "").strip().lower()


def _provider_error_code(value: Any) -> str:
    if not isinstance(value, Mapping):
        return ""
    return str(value.get("error_code") or value.get("reason") or "").strip()


class ArbitrageEngine:
    def __init__(
        self,
        store: ArbitrageStore,
        *,
        dex_adapter: DexSwapSubmitAdapter | None = None,
        bridge_adapter: BridgeSubmitAdapter | None = None,
        cex_adapter: CexTradeAdapter | None = None,
    ):
        self.store = store
        self.dex_adapter = dex_adapter
        self.bridge_adapter = bridge_adapter
        self.cex_adapter = cex_adapter

    def seed_demo_sol_opportunity(self) -> dict[str, Any]:
        return seed_demo_sol_opportunity(self.store)

    def run_precheck(self, *, opportunity_id: int, route_id: int, checks: list[dict[str, Any]]) -> dict[str, Any]:
        opportunity = self.store.get_opportunity(opportunity_id)
        if not opportunity:
            return {"ok": False, "error_code": "opportunity_not_found"}
        route = self.store.get_route(route_id)
        if not route:
            return {"ok": False, "error_code": "route_not_found"}
        if int(route.get("opportunity_id") or 0) != int(opportunity_id):
            return {"ok": False, "error_code": "route_opportunity_mismatch"}

        normalized_checks = _normalize_precheck_checks(checks)
        blockers: list[str] = []
        warnings: list[str] = []
        has_error = False
        has_block = False
        for check in normalized_checks:
            status = str(check["status"])
            name = str(check["check_name"])
            if status == "ERROR":
                has_error = True
                blockers.append(str(check.get("error_code") or name))
            elif status == "BLOCK":
                has_block = True
                blockers.append(str(check.get("error_code") or name))
            elif status == "WARN":
                warnings.append(str(check.get("error_code") or name))

        edge_verified = int(route.get("edge_worst_verified") or 0) == 1
        if has_error:
            status = "ERROR"
            route_status = "BLOCKED"
        elif has_block:
            status = "BLOCK"
            route_status = "BLOCKED"
        elif warnings:
            status = "WARN"
            route_status = "WARN"
        elif edge_verified:
            status = "PASS"
            route_status = "OPEN"
        else:
            status = "WARN"
            route_status = "WARN"
            warnings.append("edge_worst_unverified")

        run_id = self.store.insert_precheck_run(
            run_key=f"precheck:{opportunity_id}:{route_id}:{now_ms()}",
            opportunity_id=opportunity_id,
            route_id=route_id,
            status=status,
        )
        for check in normalized_checks:
            self.store.insert_precheck_result(
                precheck_run_id=run_id,
                check_name=str(check["check_name"]),
                status=str(check["status"]),
                error_code=str(check.get("error_code") or ""),
                error_msg=str(check.get("error_msg") or ""),
                details=_precheck_details(check.get("details")),
            )
        self.store.set_route_precheck_status(
            route_id,
            safety_status=status,
            route_status=route_status,
            blockers=blockers,
            warnings=warnings,
        )
        self.store.append_event(
            event_type="flow.node.update",
            opportunity_id=opportunity_id,
            route_id=route_id,
            payload={"node": "precheck", "status": status, "blockers": blockers, "warnings": warnings},
        )
        return {"ok": True, "precheck_run_id": run_id, "status": status, "route_status": route_status}

    def start_execution(
        self,
        *,
        opportunity_id: int,
        route_id: int,
        mode: str,
        idempotency_key: str,
        requested_by: str = "system",
        trade_amount_krw: float | None = None,
        execution_policy: str | None = None,
    ) -> dict[str, Any]:
        mode_key = str(mode or "paper").strip() or "paper"
        existing = self.store.get_execution_by_idempotency(idempotency_key)
        if existing:
            if idempotency_scope_conflict(
                existing,
                opportunity_id=opportunity_id,
                route_id=route_id,
                mode=mode_key,
                trade_amount_krw=trade_amount_krw,
            ):
                return idempotency_conflict_response(existing)
            return existing_run_response(existing, non_ok_statuses=NON_OK_EXISTING_RUN_STATUSES)

        route = self.store.get_route(route_id) or {}
        approval_gate: dict[str, Any] | None = None
        if mode_key == "one_click" and route and int(route.get("opportunity_id") or 0) == int(opportunity_id):
            approval_gate = self._one_click_approval_gate(
                opportunity_id=opportunity_id,
                route_id=route_id,
                route=route,
                requested_by=requested_by,
                trade_amount_krw=trade_amount_krw,
            )
        elif (
            mode_key == "live_full"
            and route
            and int(route.get("opportunity_id") or 0) == int(opportunity_id)
            and str(route.get("route_type") or "") in LIVE_FULL_ROUTE_TYPES
        ):
            approval_gate = self._live_full_approval_gate(
                opportunity_id=opportunity_id,
                route_id=route_id,
                requested_by=requested_by,
                trade_amount_krw=trade_amount_krw,
            )

        blockers = self._execution_blockers(
            opportunity_id=opportunity_id,
            route_id=route_id,
            mode=mode_key,
            trade_amount_krw=trade_amount_krw,
        )
        if approval_gate and approval_gate.get("blocker"):
            _append_unique(blockers, str(approval_gate["blocker"]))
        if approval_gate:
            for approval_blocker in _as_list(approval_gate.get("blockers")):
                _append_unique(blockers, str(approval_blocker))
        if mode_key == "one_click" and approval_gate and approval_gate.get("status") == "required":
            _append_unique(blockers, "approval_required")
            approval = dict(approval_gate.get("approval") or {})
            return {
                "ok": False,
                "existing": False,
                "error_code": "approval_required",
                "approval_required": True,
                "approval": approval,
                "blockers": blockers,
            }
        if blockers:
            blocked_payload: dict[str, Any] = {"blockers": blockers, "trade_amount_krw": trade_amount_krw}
            if approval_gate:
                blocked_payload["approval"] = self._approval_metadata(dict(approval_gate.get("approval") or {}))
            run = self.store.insert_execution_run(
                execution_key=f"exec:{uuid.uuid4().hex}",
                idempotency_key=idempotency_key,
                opportunity_id=opportunity_id,
                route_id=route_id,
                mode=mode_key,
                status="BLOCKED",
                requested_by=requested_by,
                error_code=",".join(blockers),
                error_msg="; ".join(blockers),
                payload=blocked_payload,
            )
            self.store.append_dead_letter(
                reason="execution_gate_failed",
                deadletter_key=f"execution_gate_failed:{idempotency_key}",
                error_code=",".join(blockers),
                payload={
                    "opportunity_id": opportunity_id,
                    "route_id": route_id,
                    "mode": mode_key,
                    "trade_amount_krw": trade_amount_krw,
                    "blockers": blockers,
                },
            )
            self.store.append_event(
                event_type="error",
                opportunity_id=opportunity_id,
                route_id=route_id,
                run_id=run["id"],
                severity="warning",
                payload={"error_code": "execution_gate_failed", "blockers": blockers},
            )
            self.store.append_event(
                event_type="execution.log.append",
                opportunity_id=opportunity_id,
                route_id=route_id,
                run_id=run["id"],
                severity="warning",
                payload={
                    "status": "BLOCKED",
                    "mode": mode_key,
                    "error_code": "execution_gate_failed",
                    "blockers": blockers,
                },
            )
            return {"ok": False, "existing": False, "run": run}

        if mode_key == "paper":
            return PaperExecutionRunner(self.store).start(
                opportunity_id=opportunity_id,
                route_id=route_id,
                mode=mode_key,
                idempotency_key=idempotency_key,
                requested_by=requested_by,
                trade_amount_krw=trade_amount_krw,
                execution_policy=execution_policy,
            )

        if mode_key == "auto_small":
            return AutoSmallSameDexDryRunRunner(self.store, adapter=self.dex_adapter).start(
                opportunity_id=opportunity_id,
                route_id=route_id,
                mode=mode_key,
                idempotency_key=idempotency_key,
                requested_by=requested_by,
                trade_amount_krw=trade_amount_krw,
            )

        if mode_key == "live_full":
            return LiveFullBridgeCexRunner(
                self.store,
                dex_adapter=self.dex_adapter,
                bridge_adapter=self.bridge_adapter,
                cex_adapter=self.cex_adapter,
                gate_checker=self._runner_gate_checker,
            ).start(
                opportunity_id=opportunity_id,
                route_id=route_id,
                mode=mode_key,
                idempotency_key=idempotency_key,
                requested_by=requested_by,
                trade_amount_krw=trade_amount_krw,
                approval=self._approval_metadata(dict((approval_gate or {}).get("approval") or {})),
                engine_gate_checked=True,
            )

        if mode_key == "one_click":
            approval = dict((approval_gate or {}).get("approval") or {})
            approval_metadata = self._approval_metadata(approval)
            run = self.store.insert_execution_run(
                execution_key=f"exec:{uuid.uuid4().hex}",
                idempotency_key=idempotency_key,
                opportunity_id=opportunity_id,
                route_id=route_id,
                mode=mode_key,
                status="EXEC_READY",
                requested_by=requested_by,
                payload={
                    "route_type": route.get("route_type"),
                    "trade_amount_krw": trade_amount_krw,
                    "held": True,
                    "non_submitting": True,
                    "approval": approval_metadata,
                },
            )
            if not run.get("created", True):
                return existing_run_response(run, non_ok_statuses=NON_OK_EXISTING_RUN_STATUSES)
            approval_id = _optional_positive_int(approval_metadata.get("approval_id"))
            if approval_id is not None:
                consumed = self.store.consume_operator_approval(approval_id, run_id=int(run["id"]))
                if not consumed or int(consumed.get("consumed_run_id") or 0) != int(run["id"]):
                    blocked = self.store.update_execution_run(
                        int(run["id"]),
                        status="BLOCKED",
                        error_code="operator_approval_already_consumed",
                        error_msg="operator approval was already consumed by another run",
                    )
                    return {"ok": False, "existing": False, "run": blocked, "error_code": "operator_approval_already_consumed"}
            for step in ROUTE_STEPS.get(str(route.get("route_type")), ROUTE_STEPS["same_dex_sell"]):
                self.store.insert_execution_step(
                    run_id=run["id"],
                    step_key=step,
                    status="PENDING",
                    payload={"held": True, "approval_id": approval_metadata.get("approval_id")},
                )
            self.store.append_event(
                event_type="execution.log.append",
                opportunity_id=opportunity_id,
                route_id=route_id,
                run_id=run["id"],
                payload={"status": "EXEC_READY", "mode": mode_key, "held": True, "approval": approval_metadata},
            )
            return {"ok": True, "existing": False, "run": run, "approval": approval, "held": True}

        run = self.store.insert_execution_run(
            execution_key=f"exec:{uuid.uuid4().hex}",
            idempotency_key=idempotency_key,
            opportunity_id=opportunity_id,
            route_id=route_id,
            mode=mode_key,
            status="ENTERING",
            requested_by=requested_by,
            payload={
                "route_type": route.get("route_type"),
                "trade_amount_krw": trade_amount_krw,
                "approval": self._approval_metadata(dict((approval_gate or {}).get("approval") or {})),
            },
        )
        if not run.get("created", True):
            return existing_run_response(run, non_ok_statuses=NON_OK_EXISTING_RUN_STATUSES)
        for step in ROUTE_STEPS.get(str(route.get("route_type")), ROUTE_STEPS["same_dex_sell"]):
            self.store.insert_execution_step(run_id=run["id"], step_key=step, status="PENDING")
        self.store.append_event(
            event_type="execution.log.append",
            opportunity_id=opportunity_id,
            route_id=route_id,
            run_id=run["id"],
            payload={"status": "ENTERING", "mode": mode_key},
        )
        return {"ok": True, "existing": False, "run": run}

    def _runner_gate_checker(
        self,
        *,
        opportunity_id: int,
        route_id: int,
        mode: str,
        trade_amount_krw: float | None,
        run_id: int | None = None,
    ) -> list[str]:
        blockers = self._execution_blockers(
            opportunity_id=opportunity_id,
            route_id=route_id,
            mode=mode,
            trade_amount_krw=trade_amount_krw,
        )
        if mode == "live_full":
            approval_gate = self._live_full_approval_gate(
                opportunity_id=opportunity_id,
                route_id=route_id,
                requested_by="runner",
                trade_amount_krw=trade_amount_krw,
                run_id=run_id,
            )
            if approval_gate.get("blocker"):
                _append_unique(blockers, str(approval_gate["blocker"]))
            for approval_blocker in _as_list(approval_gate.get("blockers")):
                _append_unique(blockers, str(approval_blocker))
        return blockers

    def _one_click_approval_gate(
        self,
        *,
        opportunity_id: int,
        route_id: int,
        route: Mapping[str, Any],
        requested_by: str,
        trade_amount_krw: float | None,
        run_id: int | None = None,
    ) -> dict[str, Any]:
        approved = self.store.list_operator_approvals(
            opportunity_id=opportunity_id,
            route_id=route_id,
            mode="one_click",
            status="APPROVED",
            limit=100,
        )
        for approval in approved:
            if int(approval.get("consumed_run_id") or 0) == int(run_id or 0):
                return {"status": "approved", "approval": approval}
            if int(approval.get("consumed_run_id") or 0) == 0:
                return {"status": "approved", "approval": approval}

        approval = self.store.get_latest_operator_approval(
            opportunity_id=opportunity_id,
            route_id=route_id,
            mode="one_click",
        )
        approval_consumed = bool(approval and int(approval.get("consumed_run_id") or 0) > 0)
        if not approval:
            approval = self.store.request_operator_approval(
                approval_key=f"operator_approval:{int(opportunity_id)}:{int(route_id)}:none:one_click",
                opportunity_id=opportunity_id,
                route_id=route_id,
                mode="one_click",
                requested_by=requested_by,
                reason="one_click execution requires operator approval",
                payload={
                    "route_type": route.get("route_type"),
                    "trade_amount_krw": trade_amount_krw,
                    "approval_required": True,
                },
            )
            return {"status": "required", "approval": approval}
        if approval_consumed:
            approval = self.store.request_operator_approval(
                approval_key=f"operator_approval:{int(opportunity_id)}:{int(route_id)}:none:one_click:{now_ms()}",
                opportunity_id=opportunity_id,
                route_id=route_id,
                mode="one_click",
                requested_by=requested_by,
                reason="one_click execution requires a fresh single-use operator approval",
                payload={
                    "route_type": route.get("route_type"),
                    "trade_amount_krw": trade_amount_krw,
                    "approval_required": True,
                    "previous_approval_consumed": True,
                },
            )
            return {"status": "required", "approval": approval}

        status = str(approval.get("status") or "").strip().upper()
        if status == "REJECTED":
            return {"status": "rejected", "approval": approval, "blocker": "operator_approval_rejected"}
        if status == "PENDING":
            return {"status": "required", "approval": approval}
        return {"status": "unknown", "approval": approval, "blocker": "operator_approval_status_unknown"}

    def _live_full_approval_gate(
        self,
        *,
        opportunity_id: int,
        route_id: int,
        requested_by: str,
        trade_amount_krw: float | None,
        run_id: int | None = None,
    ) -> dict[str, Any]:
        requested_amount = _optional_positive_float(trade_amount_krw)
        if requested_amount is None:
            return {"status": "missing_amount", "blocker": "operator_approval_required"}

        current_ms = now_ms()
        approval = self.store.find_matching_operator_approval(
            opportunity_id=opportunity_id,
            route_id=route_id,
            mode="live_full",
            trade_amount_krw=requested_amount,
            now_at_ms=current_ms,
            allow_consumed_run_id=run_id,
        )
        if approval:
            return {"status": "approved", "approval": approval}

        latest = self.store.get_latest_operator_approval(
            opportunity_id=opportunity_id,
            route_id=route_id,
            mode="live_full",
        )
        if not latest:
            return {"status": "required", "blocker": "operator_approval_required"}

        status = str(latest.get("status") or "").strip().upper()
        if status == "REJECTED":
            return {"status": "rejected", "approval": latest, "blocker": "operator_approval_rejected"}
        if status == "PENDING":
            return {"status": "required", "approval": latest, "blocker": "operator_approval_required"}
        if status != "APPROVED":
            return {"status": "unknown", "approval": latest, "blocker": "operator_approval_status_unknown"}

        blockers: list[str] = []
        amount = _approval_amount_krw(latest)
        if amount is None:
            blockers.append("operator_approval_amount_missing")
        elif abs(float(amount) - requested_amount) > 0.000001:
            blockers.append("operator_approval_amount_mismatch")
        expires_at_ms = _approval_expires_at_ms(latest)
        if expires_at_ms is None:
            blockers.append("operator_approval_window_missing")
        elif int(expires_at_ms) <= current_ms:
            blockers.append("operator_approval_expired")
        if not blockers:
            blockers.append("operator_approval_required")
        return {
            "status": "mismatch",
            "approval": latest,
            "requested_by": requested_by,
            "blockers": blockers,
        }

    def _approval_metadata(self, approval: Mapping[str, Any]) -> dict[str, Any]:
        if not approval:
            return {}
        return {
            "approval_id": approval.get("id"),
            "approval_key": approval.get("approval_key"),
            "approval_status": approval.get("status"),
            "mode": approval.get("mode"),
            "requested_by": approval.get("requested_by"),
            "reason": approval.get("reason"),
            "operator": approval.get("operator"),
            "requested_at_ms": approval.get("requested_at_ms"),
            "decided_at_ms": approval.get("decided_at_ms"),
            "decision_payload": approval.get("decision_payload"),
        }

    def mark_unknown_outcome(self, *, run_id: int, step_key: str, external_ref: str, error_code: str) -> dict[str, Any]:
        run = self.store.get_execution_run(run_id)
        if not run:
            raise ValueError("execution_run_not_found")
        step = self.store.update_execution_step(
            run_id=run_id,
            step_key=step_key,
            status="RECONCILE",
            external_ref=external_ref,
            error_code=error_code,
        )
        updated = self.store.update_execution_run(run_id, status="MANUAL_REVIEW", error_code=error_code)
        self.store.append_dead_letter(
            reason="unknown_external_outcome",
            deadletter_key=f"unknown_external_outcome:{run_id}:{step_key}",
            error_code=error_code,
            retryable=False,
            payload={"run_id": run_id, "step_key": step_key, "external_ref": external_ref},
        )
        self.store.append_event(
            event_type="error",
            opportunity_id=run["opportunity_id"],
            route_id=run["route_id"],
            run_id=run_id,
            severity="error",
            payload={"step_key": step_key, "status": "RECONCILE", "error_code": error_code},
        )
        return {"run": updated, "step": step}

    def abort_execution(self, run_id: int) -> dict[str, Any]:
        existing = self.store.get_execution_run(run_id)
        if not existing:
            return {"ok": False, "error_code": "execution_run_not_found", "run": None}
        if str(existing.get("status") or "") in TERMINAL_EXECUTION_STATUSES:
            return {"ok": False, "error_code": "execution_run_terminal", "run": existing}
        run = self.store.update_execution_run(run_id, status="ABORTED", error_code="operator_abort")
        self.store.append_event(
            event_type="execution.log.append",
            opportunity_id=run["opportunity_id"],
            route_id=run["route_id"],
            run_id=run_id,
            severity="warning",
            payload={"status": "ABORTED"},
        )
        return {"ok": True, "run": run}

    def snapshot(self, *, selected_opportunity_id: int | None = None) -> dict[str, Any]:
        opportunities = self.store.fetch_opportunities()
        selected_id = selected_opportunity_id or (opportunities[0]["id"] if opportunities else None)
        routes = self.store.fetch_routes_for_opportunity(selected_id) if selected_id else []
        selected_route_id = self._selected_route_id(selected_id, opportunities, routes)
        selected_route = self._selected_route_snapshot(
            next((route for route in routes if int(route["id"]) == int(selected_route_id or 0)), None)
        )
        selected_run = self._selected_execution_run(selected_id, selected_route_id)
        selected_paper_run = self._selected_paper_run(selected_id, selected_route_id)
        selected_run_id = int(selected_run["id"]) if selected_run else None
        logs = list(
            reversed(
                self.store.fetch_event_log(
                    limit=100,
                    opportunity_id=int(selected_id) if selected_id else None,
                    run_id=selected_run_id,
                )
            )
        )
        execution_steps = self.store.fetch_execution_steps(int(selected_run["id"])) if selected_run else []
        current_step = self._current_execution_step(execution_steps)
        positions = self._snapshot_positions(selected_id=selected_id, selected_run=selected_run)
        transactions = self.store.fetch_transactions_for_run_step(int(selected_run["id"])) if selected_run else []
        orders = self.store.fetch_orders_for_run_step(int(selected_run["id"])) if selected_run else []
        transfers = self.store.fetch_transfers_for_run_step(int(selected_run["id"])) if selected_run else []
        alert_filter = {"opportunity_id": int(selected_id)} if selected_id else {}
        return {
            "server_time": now_ms(),
            "snapshot_seq": self.store.latest_event_seq(),
            "mode": "backend_contract",
            "provider_health": self.store.fetch_provider_health(),
            "opportunities": [self._opportunity_card(row) for row in opportunities],
            "selected_opportunity_id": selected_id,
            "selected_route_id": selected_route_id,
            "selected_route": selected_route,
            "current_route_type": (selected_route or {}).get("route_type"),
            "approval_status": (selected_route or {}).get("approval_status"),
            "blockers": self._snapshot_blockers(selected_route=selected_route, selected_run=selected_run),
            "live_full_boundary": self._live_full_boundary_snapshot(
                selected_route=selected_route,
                selected_run=selected_run,
            ),
            "selected_execution_run": selected_run,
            "selected_paper_run": selected_paper_run,
            "current_step": current_step,
            "current_step_key": current_step.get("step_key") if current_step else None,
            "execution_steps": execution_steps,
            "flow_nodes": self._flow_nodes(selected_id, routes, selected_run=selected_run, execution_steps=execution_steps),
            "flow_edges": self._flow_edges(routes, selected_run=selected_run, execution_steps=execution_steps),
            "pending_approvals": self.store.list_operator_approvals(
                opportunity_id=int(selected_id) if selected_id else None,
                route_id=int(selected_route_id) if selected_route_id else None,
                status="PENDING",
                limit=100,
            ),
            "alerts": self.store.fetch_alerts(channel="db_sse", limit=100, **alert_filter),
            "logs": logs,
            "positions": positions,
            "transactions": transactions,
            "orders": orders,
            "transfers": transfers,
        }

    def _snapshot_blockers(
        self,
        *,
        selected_route: Mapping[str, Any] | None,
        selected_run: Mapping[str, Any] | None,
    ) -> list[str]:
        blockers: list[str] = []
        route = selected_route or {}
        for reason in _as_list(route.get("blocker_reasons")):
            _append_unique(blockers, str(reason))
        route_payload = _as_mapping(route.get("payload"))
        for reason in _as_list(route_payload.get("blockers")):
            _append_unique(blockers, str(reason))
        if selected_run:
            run_payload = _as_mapping(selected_run.get("payload"))
            for reason in _as_list(run_payload.get("blockers")):
                _append_unique(blockers, str(reason))
            for reason in str(selected_run.get("error_code") or "").split(","):
                _append_unique(blockers, reason.strip())
        return blockers

    def _live_full_boundary_snapshot(
        self,
        *,
        selected_route: Mapping[str, Any] | None,
        selected_run: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        route = selected_route or {}
        route_type = str(route.get("route_type") or "")
        run_payload = _as_mapping((selected_run or {}).get("payload"))
        return {
            "mode": (selected_run or {}).get("mode") or "",
            "route_type": route_type,
            "dry_run": bool(run_payload.get("dry_run", route_type in LIVE_FULL_ROUTE_TYPES)),
            "simulated": bool(run_payload.get("simulated", route_type in LIVE_FULL_ROUTE_TYPES)),
            "provider_boundary": run_payload.get("adapter_boundary") or "deterministic_default_or_explicit_adapter",
            "real_external_submit_enabled": False,
            "cex_withdrawal_enabled": False,
            "dex_adapter_name": run_payload.get("dex_adapter_name") or "",
            "bridge_adapter_name": run_payload.get("bridge_adapter_name") or "",
            "cex_adapter_name": run_payload.get("cex_adapter_name") or "",
        }

    def _selected_route_id(
        self,
        selected_id: int | None,
        opportunities: list[dict[str, Any]],
        routes: list[dict[str, Any]],
    ) -> int | None:
        if not selected_id:
            return None
        opportunity = next((row for row in opportunities if int(row["id"]) == int(selected_id)), None)
        if opportunity and opportunity.get("selected_route_id"):
            return int(opportunity["selected_route_id"])
        selected_route = next((route for route in routes if int(route.get("selected") or 0) == 1), None)
        if selected_route:
            return int(selected_route["id"])
        return int(routes[0]["id"]) if routes else None

    def _selected_execution_run(self, selected_id: int | None, selected_route_id: int | None) -> dict[str, Any] | None:
        if not selected_id:
            return None
        if selected_route_id:
            route_runs = self.store.fetch_execution_runs(
                opportunity_id=selected_id,
                route_id=selected_route_id,
                limit=1,
            )
            if route_runs:
                return route_runs[0]
        runs = self.store.fetch_execution_runs(opportunity_id=selected_id, limit=1)
        return runs[0] if runs else None

    def _selected_paper_run(self, selected_id: int | None, selected_route_id: int | None) -> dict[str, Any] | None:
        if not selected_id:
            return None
        if selected_route_id:
            route_runs = self.store.fetch_execution_runs(
                opportunity_id=selected_id,
                route_id=selected_route_id,
                mode="paper",
                limit=1,
            )
            if route_runs:
                return route_runs[0]
        runs = self.store.fetch_execution_runs(opportunity_id=selected_id, mode="paper", limit=1)
        return runs[0] if runs else None

    def _current_execution_step(self, steps: list[dict[str, Any]]) -> dict[str, Any] | None:
        for status in ("RUNNING", "RECONCILE"):
            step = next((row for row in steps if str(row.get("status")) == status), None)
            if step:
                return step
        pending = next((row for row in steps if str(row.get("status")) == "PENDING"), None)
        if pending:
            return pending
        return steps[-1] if steps else None

    def _snapshot_positions(
        self,
        *,
        selected_id: int | None,
        selected_run: Mapping[str, Any] | None,
    ) -> list[dict[str, Any]]:
        if selected_run:
            positions = self.store.fetch_positions(run_id=int(selected_run["id"]))
        elif selected_id:
            positions = self.store.fetch_positions(opportunity_id=selected_id)
        else:
            positions = self.store.fetch_positions()
        for position in positions:
            marks = self.store.fetch_position_marks(int(position["id"]))
            position["marks"] = marks
            position["latest_mark"] = marks[-1] if marks else None
        return positions

    def _execution_blockers(
        self,
        *,
        opportunity_id: int,
        route_id: int,
        mode: str,
        trade_amount_krw: float | None = None,
    ) -> list[str]:
        blockers: list[str] = []
        mode_key = str(mode)
        profile = self.store.get_strategy_profile("default")
        opportunity = self.store.get_opportunity(opportunity_id)
        route = self.store.get_route(route_id)
        current_ms = now_ms()

        if not opportunity:
            return ["opportunity_not_found"]
        if not route:
            return ["route_not_found"]
        if int(route.get("opportunity_id") or 0) != int(opportunity_id):
            blockers.append("route_opportunity_mismatch")
        if mode_key == "auto_small" and str(route.get("route_type") or "") != "same_dex_sell":
            blockers.append("route_type_not_supported")
        if mode_key == "live_full" and str(route.get("route_type") or "") not in LIVE_FULL_ROUTE_TYPES:
            blockers.append("route_type_not_supported")
        if self.store.is_kill_switch_active():
            blockers.append("kill_switch_active")
        if not profile or int(profile.get("active") or 0) != 1:
            blockers.append("strategy_inactive")
        elif int(profile.get(f"{mode_key}_enabled") or 0) != 1:
            blockers.append(f"{mode_key}_disabled")
        if mode_key in EDGE_GATED_MODES and profile:
            max_trade = float(profile.get("max_trade_krw") or 0)
            max_daily_loss = float(profile.get("max_daily_loss_krw") or 0)
            if max_trade <= 0:
                blockers.append("trade_cap_not_configured")
            if max_daily_loss <= 0:
                blockers.append("daily_loss_cap_not_configured")
            if trade_amount_krw is None or float(trade_amount_krw) <= 0:
                blockers.append("trade_amount_missing")
            elif max_trade > 0 and float(trade_amount_krw) > max_trade:
                blockers.append("trade_amount_exceeds_cap")
        if str(opportunity.get("safety_status")) != "PASS":
            blockers.append("opportunity_safety_not_pass")
        if str(route.get("safety_status")) != "PASS":
            blockers.append("route_safety_not_pass")
        if str(route.get("route_status")) == "BLOCKED":
            blockers.append("route_blocked")
        if str(route.get("safety_status")) in {"BLOCK", "ERROR"}:
            blockers.append("precheck_blocked")
        if str(route.get("route_status")) not in {"OPEN", "DONE"}:
            blockers.append(f"route_status_{route.get('route_status')}")
        if float(route.get("edge_worst_bps") or 0) <= float((profile or {}).get("min_edge_worst_bps") or 0):
            blockers.append("edge_worst_below_threshold")
        if mode_key in EDGE_GATED_MODES and int(route.get("edge_worst_verified") or 0) != 1:
            blockers.append("edge_worst_unverified")
        if mode_key in EDGE_GATED_MODES and (
            _simulation_evidence_not_executable(route)
            or self._latest_simulation_evidence_not_executable(route)
        ):
            blockers.append("simulation_evidence_not_executable")
        fresh_until = route.get("quote_fresh_until_ms")
        if fresh_until is None or int(fresh_until) < current_ms:
            blockers.append("stale_quote")
        if mode_key in EDGE_GATED_MODES:
            freshness = self.store.fetch_route_freshness(route_id)
            self._append_edge_evidence_blockers(
                blockers,
                route=route,
                freshness=freshness,
                current_ms=current_ms,
            )
        if route.get("blocker_reasons"):
            blockers.append("route_blockers_present")
        if mode_key == "live_full" and str(route.get("route_type") or "") in LIVE_FULL_ROUTE_TYPES:
            self._append_live_full_route_blockers(blockers, route=route)
        has_wallet, wallet_reason = self.store.has_execution_wallet(mode, route_id=route_id)
        if not has_wallet:
            blockers.append(wallet_reason)
        return blockers

    def _append_live_full_route_blockers(self, blockers: list[str], *, route: Mapping[str, Any]) -> None:
        route_type = str(route.get("route_type") or "")
        payload = _as_mapping(route.get("payload"))
        sell_market = self.store.get_market_detail(int(route.get("sell_market_id") or 0)) or {}

        if route_type in CEX_ROUTE_TYPES and not str(sell_market.get("deposit_network") or "").strip():
            _append_unique(blockers, "missing_deposit_network")

        if route_type in CEX_ROUTE_TYPES:
            self._append_provider_status_blockers(
                blockers,
                payload=payload,
                keys=("cex_deposit_status", "deposit_status", "cex_deposit"),
                blocked_reason="cex_deposit_blocked",
                scope="cex_deposit",
            )

        if route_type in BRIDGE_ROUTE_TYPES:
            self._append_provider_status_blockers(
                blockers,
                payload=payload,
                keys=("bridge_status", "bridge_availability", "bridge_route", "bridge"),
                blocked_reason="bridge_route_unavailable",
                scope="bridge",
            )
            if _explicit_false(payload.get("bridge_fee_verified")):
                _append_unique(blockers, "bridge_fee_unverified")
            for key in ("bridge_fee", "bridge_quote"):
                record = _as_mapping(payload.get(key))
                if _explicit_false(record.get("verified")) or _explicit_false(record.get("fee_verified")):
                    _append_unique(blockers, "bridge_fee_unverified")

    def _latest_simulation_evidence_not_executable(self, route: Mapping[str, Any]) -> bool:
        route_id = int(route.get("id") or 0)
        if route_id:
            with self.store.conn() as conn:
                quote_rows = conn.execute(
                    """
                    SELECT leg_type, source
                    FROM arb_route_quotes
                    WHERE route_id = ?
                    ORDER BY observed_at_ms DESC, id DESC
                    """,
                    (route_id,),
                ).fetchall()
            latest_quote_by_leg: dict[str, str] = {}
            for row in quote_rows:
                leg_type = str(row["leg_type"] or "")
                if leg_type not in latest_quote_by_leg:
                    latest_quote_by_leg[leg_type] = str(row["source"] or "")
            if any(source == "no_funds_simulation" for source in latest_quote_by_leg.values()):
                return True
        market_ids = [
            int(value)
            for value in (route.get("buy_market_id"), route.get("sell_market_id"))
            if value not in (None, "")
        ]
        if not market_ids:
            return False
        placeholders = ",".join("?" for _ in market_ids)
        with self.store.conn() as conn:
            rows = conn.execute(
                f"""
                SELECT market_id, source
                FROM arb_market_ticks
                WHERE market_id IN ({placeholders})
                ORDER BY observed_at_ms DESC, id DESC
                """,
                tuple(market_ids),
            ).fetchall()
        latest_by_market: dict[int, str] = {}
        for row in rows:
            market_id = int(row["market_id"])
            if market_id not in latest_by_market:
                latest_by_market[market_id] = str(row["source"] or "")
        return any(source == "no_funds_simulation" for source in latest_by_market.values())

    def _append_provider_status_blockers(
        self,
        blockers: list[str],
        *,
        payload: Mapping[str, Any],
        keys: tuple[str, ...],
        blocked_reason: str,
        scope: str,
    ) -> None:
        for key, value in _iter_payload_status_values(payload, keys):
            status = _provider_status_text(value)
            if not status:
                continue
            error_code = _provider_error_code(value)
            if status in PROVIDER_STATUS_PASS:
                continue
            if status in PROVIDER_STATUS_BLOCK:
                _append_unique(blockers, blocked_reason)
                if error_code:
                    _append_unique(blockers, error_code)
            elif status in PROVIDER_STATUS_PENDING:
                _append_unique(blockers, f"provider_status_pending:{scope}")
            elif status in PROVIDER_STATUS_UNKNOWN:
                _append_unique(blockers, f"provider_status_unknown:{scope}")
            else:
                _append_unique(blockers, f"provider_status_unknown:{scope}")
            _append_unique(blockers, f"{key}:{status}")

    def _append_edge_evidence_blockers(
        self,
        blockers: list[str],
        *,
        route: Mapping[str, Any],
        freshness: Mapping[str, int],
        current_ms: int,
    ) -> None:
        payload = _as_mapping(route.get("payload"))
        edge_evaluation = _as_mapping(payload.get("edge_evaluation"))
        evaluated_components: set[str] = set()

        for component in _as_list(edge_evaluation.get("missing_components")):
            component_name = str(component)
            evaluated_components.add(component_name)
            _append_unique(blockers, f"edge_component_missing:{component_name}")
        for component in _as_list(edge_evaluation.get("stale_components")):
            component_name = str(component)
            evaluated_components.add(component_name)
            _append_unique(blockers, f"edge_component_stale:{component_name}")

        for raw_component, raw_record in _as_mapping(edge_evaluation.get("freshness")).items():
            record = _as_mapping(raw_record)
            component_name = str(record.get("component") or raw_component)
            evaluated_components.add(component_name)
            status = str(record.get("status") or "").lower()
            if status == "missing":
                _append_unique(blockers, f"edge_component_missing:{component_name}")
            elif status == "stale":
                _append_unique(blockers, f"edge_component_stale:{component_name}")
            elif status == "unknown":
                _append_unique(blockers, f"edge_component_freshness_unknown:{component_name}")

        for reason in _as_list(route.get("blocker_reasons")):
            reason_text = str(reason)
            if reason_text.startswith(
                (
                    "edge_component_missing:",
                    "edge_component_stale:",
                    "edge_component_freshness_unknown:",
                )
            ):
                _append_unique(blockers, reason_text)

        self._append_route_freshness_blockers(
            blockers,
            route=route,
            freshness=freshness,
            current_ms=current_ms,
            edge_evaluation_present=bool(edge_evaluation),
            evaluated_components=evaluated_components,
        )

    def _append_route_freshness_blockers(
        self,
        blockers: list[str],
        *,
        route: Mapping[str, Any],
        freshness: Mapping[str, int],
        current_ms: int,
        edge_evaluation_present: bool,
        evaluated_components: set[str],
    ) -> None:
        normalized_freshness = {str(source).strip().lower(): int(fresh_until) for source, fresh_until in freshness.items()}
        for source_key, fresh_until_ms in normalized_freshness.items():
            if fresh_until_ms >= current_ms:
                continue
            component = _freshness_source_component(source_key)
            _append_unique(blockers, f"edge_component_stale:{component}")
            _append_unique(blockers, f"stale_{source_key}")

        for source_group in self._required_freshness_groups(route):
            normalized_group = tuple(str(source).strip().lower() for source in source_group)
            component = _freshness_source_component(normalized_group[0])
            if any(source in normalized_freshness for source in normalized_group):
                continue
            _append_unique(blockers, f"edge_component_missing:{component}")
            _append_unique(blockers, f"missing_{normalized_group[0]}_freshness")

    def _required_freshness_groups(self, route: Mapping[str, Any]) -> list[tuple[str, ...]]:
        route_type = str(route.get("route_type") or "")
        groups = list(REQUIRED_FRESHNESS_BY_ROUTE_TYPE.get(route_type, ()))
        if route_type in KRW_FX_ROUTE_TYPES:
            sell_market = self.store.get_market_detail(int(route.get("sell_market_id") or 0)) or {}
            if str(sell_market.get("quote_asset") or "").upper() == "KRW":
                groups.append(("fx",))
        return groups

    def _opportunity_card(self, row: dict[str, Any]) -> dict[str, Any]:
        buy = self.store.get_market_detail(int(row["buy_market_id"])) or {}
        sell = self.store.get_market_detail(int(row["sell_market_id"])) or {}
        selected_route = self.store.get_route(int(row["selected_route_id"])) if row.get("selected_route_id") else None
        selected_route = self._selected_route_snapshot(selected_route)
        return {
            "id": row["id"],
            "symbol": row.get("symbol"),
            "status": row.get("lifecycle_status"),
            "safety_status": row.get("safety_status"),
            "spread_bps": row.get("spread_bps"),
            "edge_worst_bps": row.get("edge_worst_bps"),
            "buy_market_id": row.get("buy_market_id"),
            "sell_market_id": row.get("sell_market_id"),
            "selected_route_id": row.get("selected_route_id"),
            "buy": buy,
            "sell": sell,
            "selected_route": selected_route or {},
        }

    def _selected_route_snapshot(self, route: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if not route:
            return None
        route_snapshot = dict(route)
        route_snapshot["freshness"] = self.store.fetch_route_freshness(int(route_snapshot["id"]))
        route_snapshot.update(self._route_approval_state(route_snapshot))
        return route_snapshot

    def _route_approval_state(self, route: Mapping[str, Any]) -> dict[str, Any]:
        opportunity_id = int(route.get("opportunity_id") or 0)
        route_id = int(route.get("id") or 0)
        latest = (
            self.store.get_latest_operator_approval(
                opportunity_id=opportunity_id,
                route_id=route_id,
                mode="one_click",
            )
            if opportunity_id and route_id
            else None
        )
        live_full_latest = (
            self.store.get_latest_operator_approval(
                opportunity_id=opportunity_id,
                route_id=route_id,
                mode="live_full",
            )
            if opportunity_id and route_id
            else None
        )
        approval_required = self._route_approval_required(route)
        approval_status = str((latest or {}).get("status") or ("MISSING" if approval_required else "NOT_REQUIRED")).upper()
        latest_metadata = self._approval_metadata(latest or {})
        live_full_metadata = self._approval_metadata(live_full_latest or {})
        live_full_required = str(route.get("route_type") or "") in LIVE_FULL_ROUTE_TYPES
        live_full_status = str(
            (live_full_latest or {}).get("status")
            or ("MISSING" if live_full_required else "NOT_REQUIRED")
        ).upper()
        return {
            "approval_required": approval_required,
            "approval_status": approval_status,
            "approval_id": (latest or {}).get("id"),
            "latest_approval": latest_metadata or None,
            "latest_approval_decision": latest_metadata if latest and latest.get("decided_at_ms") else None,
            "live_full_approval_required": live_full_required,
            "live_full_approval_status": live_full_status,
            "live_full_approval_id": (live_full_latest or {}).get("id"),
            "live_full_latest_approval": live_full_metadata or None,
            "live_full_latest_approval_decision": (
                live_full_metadata if live_full_latest and live_full_latest.get("decided_at_ms") else None
            ),
            "live_full_approval_amount_krw": _approval_amount_krw(live_full_latest or {}),
            "live_full_approval_expires_at_ms": _approval_expires_at_ms(live_full_latest or {}),
        }

    def _route_approval_required(self, route: Mapping[str, Any]) -> bool:
        payload = _as_mapping(route.get("payload"))
        explicit = payload.get("approval_required")
        if isinstance(explicit, bool):
            return explicit
        if isinstance(explicit, (int, float)):
            return bool(explicit)
        if isinstance(explicit, str) and explicit.strip():
            return explicit.strip().lower() in {"1", "true", "yes", "required"}
        return True

    def _flow_nodes(
        self,
        selected_id: int | None,
        routes: list[dict[str, Any]],
        *,
        selected_run: Mapping[str, Any] | None = None,
        execution_steps: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        if not selected_id:
            return []
        opportunity = self.store.get_opportunity(selected_id) or {}
        route_by_id = {int(route["id"]): route for route in routes}
        selected_route_id = int(selected_run["route_id"]) if selected_run else _selected_route_id_from_routes(routes)
        selected_route = route_by_id.get(selected_route_id or 0)
        selected_route_type = str((selected_route or {}).get("route_type") or "same_dex_sell")
        selected_run_payload = _as_mapping((selected_run or {}).get("payload"))
        buy_then_hold = selected_run_payload.get("execution_policy") == "buy_then_hold"
        steps = execution_steps or []
        precheck = UI_STATUS.get(str(opportunity.get("safety_status")), "wait")
        nodes = [
            {
                "id": "signal",
                "state": "done" if selected_run else UI_STATUS.get(str(opportunity.get("lifecycle_status")), "wait"),
                "status": opportunity.get("lifecycle_status"),
                "detail": opportunity.get("anomaly_type"),
                "route_id": selected_route_id,
                "run_id": int(selected_run["id"]) if selected_run else None,
                "duration_ms": None,
            },
            self._execution_node(
                "precheck",
                route_type=selected_route_type,
                route_id=selected_route_id,
                run_id=int(selected_run["id"]) if selected_run else None,
                steps=steps,
                fallback_state=precheck,
                fallback_status=opportunity.get("safety_status"),
                detail=opportunity.get("safety_status"),
            ),
            self._execution_node(
                "dexBuy",
                route_type=selected_route_type,
                route_id=selected_route_id,
                run_id=int(selected_run["id"]) if selected_run else None,
                steps=steps,
                fallback_state="wait",
                fallback_status="PENDING",
                detail="execution gate pending",
            ),
        ]
        added = {node["id"] for node in nodes}
        for route in routes:
            route_type = str(route["route_type"])
            run_id = int(selected_run["id"]) if selected_run and int(route["id"]) == selected_route_id else None
            route_steps = steps if run_id else []
            route_fallback_state = "skipped" if buy_then_hold and run_id else UI_STATUS.get(str(route["route_status"]), "wait")
            route_fallback_status = "SKIPPED" if buy_then_hold and run_id else route["route_status"]
            for node_id in route_node_ids(route_type):
                if node_id in added:
                    continue
                added.add(node_id)
                nodes.append(
                    self._execution_node(
                        node_id,
                        route_type=route_type,
                        route_id=int(route["id"]),
                        run_id=run_id,
                        steps=route_steps,
                        fallback_state=route_fallback_state,
                        fallback_status=route_fallback_status,
                        detail=route["safety_status"],
                    )
                )
        if buy_then_hold and "walletHold" not in added:
            nodes.append(
                self._execution_node(
                    "walletHold",
                    route_type=selected_route_type,
                    route_id=selected_route_id,
                    run_id=int(selected_run["id"]) if selected_run else None,
                    steps=steps,
                    fallback_state="wait",
                    fallback_status="PENDING",
                    detail="매수 후 지갑보유",
                )
            )
        return nodes

    def _flow_edges(
        self,
        routes: list[dict[str, Any]],
        *,
        selected_run: Mapping[str, Any] | None = None,
        execution_steps: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        route_by_id = {int(route["id"]): route for route in routes}
        selected_route_id = int(selected_run["route_id"]) if selected_run else _selected_route_id_from_routes(routes)
        selected_route = route_by_id.get(selected_route_id or 0)
        selected_route_type = str((selected_route or {}).get("route_type") or "same_dex_sell")
        selected_run_payload = _as_mapping((selected_run or {}).get("payload"))
        buy_then_hold = selected_run_payload.get("execution_policy") == "buy_then_hold"
        steps = execution_steps or []
        run_id = int(selected_run["id"]) if selected_run else None
        base = [
            self._execution_edge(
                "signal-precheck",
                route_type=selected_route_type,
                route_id=selected_route_id,
                run_id=run_id,
                steps=steps,
                fallback_state="done",
                fallback_status="DONE",
            ),
            self._execution_edge(
                "precheck-buy",
                route_type=selected_route_type,
                route_id=selected_route_id,
                run_id=run_id,
                steps=steps,
                fallback_state="wait",
                fallback_status="PENDING",
            ),
        ]
        added = {edge["id"] for edge in base}
        for route in routes:
            route_type = str(route["route_type"])
            route_run_id = run_id if selected_run and int(route["id"]) == selected_route_id else None
            route_steps = steps if route_run_id else []
            route_fallback_state = "skipped" if buy_then_hold and route_run_id else UI_STATUS.get(str(route["route_status"]), "wait")
            route_fallback_status = "SKIPPED" if buy_then_hold and route_run_id else route["route_status"]
            for edge_id in route_edge_ids(route_type):
                if edge_id in added:
                    continue
                added.add(edge_id)
                base.append(
                    self._execution_edge(
                        edge_id,
                        route_type=route_type,
                        route_id=int(route["id"]),
                        run_id=route_run_id,
                        steps=route_steps,
                        fallback_state=route_fallback_state,
                        fallback_status=route_fallback_status,
                    )
                )
        if buy_then_hold and "buy-wallet-hold" not in added:
            base.append(
                self._execution_edge(
                    "buy-wallet-hold",
                    route_type=selected_route_type,
                    route_id=selected_route_id,
                    run_id=run_id,
                    steps=steps,
                    fallback_state="wait",
                    fallback_status="PENDING",
                )
            )
        return base

    def _execution_node(
        self,
        node_id: str,
        *,
        route_type: str,
        route_id: int | None,
        run_id: int | None,
        steps: list[dict[str, Any]],
        fallback_state: str,
        fallback_status: Any,
        detail: Any,
    ) -> dict[str, Any]:
        node_steps = [step for step in steps if step_node_id(route_type, str(step.get("step_key"))) == node_id]
        state, status = _flow_state_from_steps(node_steps, fallback_state=fallback_state, fallback_status=str(fallback_status or ""))
        timing = _flow_timing(node_steps)
        return {
            "id": node_id,
            "state": state,
            "status": status,
            "detail": detail,
            "route_id": route_id,
            "run_id": run_id,
            "step_keys": [str(step.get("step_key")) for step in node_steps],
            "external_refs": unique_ordered([
                str(step.get("external_ref") or "") for step in node_steps if step.get("external_ref")
            ]),
            **timing,
        }

    def _execution_edge(
        self,
        edge_id: str,
        *,
        route_type: str,
        route_id: int | None,
        run_id: int | None,
        steps: list[dict[str, Any]],
        fallback_state: str,
        fallback_status: Any,
    ) -> dict[str, Any]:
        edge_steps = [step for step in steps if step_edge_id(route_type, str(step.get("step_key"))) == edge_id]
        state, status = _flow_state_from_steps(edge_steps, fallback_state=fallback_state, fallback_status=str(fallback_status or ""))
        source, target = flow_edge_endpoints(edge_id)
        timing = _flow_timing(edge_steps)
        return {
            "id": edge_id,
            "source": source,
            "target": target,
            "state": state,
            "status": status,
            "route_id": route_id,
            "run_id": run_id,
            "step_keys": [str(step.get("step_key")) for step in edge_steps],
            "external_refs": unique_ordered([
                str(step.get("external_ref") or "") for step in edge_steps if step.get("external_ref")
            ]),
            **timing,
        }


def _flow_state_from_steps(
    steps: list[dict[str, Any]],
    *,
    fallback_state: str,
    fallback_status: str,
) -> tuple[str, str]:
    if not steps:
        return fallback_state, fallback_status
    statuses = [str(step.get("status") or "PENDING") for step in steps]
    if "RECONCILE" in statuses:
        return "warn", "RECONCILE"
    if "FAILED" in statuses:
        return "failed", "FAILED"
    if "BLOCKED" in statuses:
        return "blocked", "BLOCKED"
    if "RUNNING" in statuses:
        return "active", "RUNNING"
    if all(status == "COMPLETED" for status in statuses):
        return "done", "COMPLETED"
    if "COMPLETED" in statuses:
        return "active", ",".join(unique_ordered(statuses))
    status = statuses[-1]
    return ui_state_for_step_status(status), status


def _flow_timing(steps: list[dict[str, Any]]) -> dict[str, Any]:
    started_values = [int(step["started_at_ms"]) for step in steps if step.get("started_at_ms") is not None]
    completed_values = [int(step["completed_at_ms"]) for step in steps if step.get("completed_at_ms") is not None]
    duration_values = [int(step["duration_ms"]) for step in steps if step.get("duration_ms") is not None]
    return {
        "started_at_ms": min(started_values) if started_values else None,
        "completed_at_ms": max(completed_values) if completed_values else None,
        "duration_ms": sum(duration_values) if duration_values else None,
    }


def _selected_route_id_from_routes(routes: list[dict[str, Any]]) -> int | None:
    selected = next((route for route in routes if int(route.get("selected") or 0) == 1), None)
    if selected:
        return int(selected["id"])
    return int(routes[0]["id"]) if routes else None
