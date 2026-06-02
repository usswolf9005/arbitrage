from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from .execution_safety import existing_run_response, idempotency_conflict_response, idempotency_scope_conflict
from .execution_flow import step_edge_id, step_node_id, ui_state_for_step_status
from .store import ArbitrageStore


ROUTE_STEPS: dict[str, list[str]] = {
    "same_dex_sell": ["precheck", "dex_buy", "exit_route_select", "same_dex_sell", "settle"],
    "bridge_dex_sell": ["precheck", "dex_buy", "exit_route_select", "bridge", "bridge_dex_sell", "settle"],
    "bridge_cex_sell": ["precheck", "dex_buy", "exit_route_select", "bridge", "cex_deposit", "cex_sell", "settle"],
    "direct_cex_sell": ["precheck", "dex_buy", "exit_route_select", "cex_deposit", "cex_sell", "settle"],
}

PAPER_STEP_DURATIONS_MS: dict[str, int] = {
    "precheck": 120,
    "dex_buy": 240,
    "wallet_hold": 80,
    "exit_route_select": 80,
    "same_dex_sell": 180,
    "bridge": 600,
    "bridge_dex_sell": 220,
    "cex_deposit": 500,
    "cex_sell": 160,
    "settle": 100,
}

EXIT_STEPS = {"same_dex_sell", "bridge_dex_sell", "cex_sell"}
TERMINAL_FAILURE_STATUSES = {"ABORTED", "BLOCKED", "FAILED", "MANUAL_REVIEW"}
# buy_then_hold paper run은 POSITION_OPEN에서 정상 종료한다. SETTLED와 함께
# terminal success로 취급해 재진입 시 중복 진행/이벤트 재발행을 막는다.
TERMINAL_SUCCESS_STATUSES = {"SETTLED", "POSITION_OPEN"}


class PaperExecutionRunner:
    """Deterministic, no-network paper execution saga runner."""

    def __init__(self, store: ArbitrageStore):
        self.store = store

    def start(
        self,
        *,
        opportunity_id: int,
        route_id: int,
        mode: str,
        idempotency_key: str,
        requested_by: str = "system",
        trade_amount_krw: float | None = None,
        simulated_outcomes: Mapping[str, str] | None = None,
        execution_policy: str | None = None,
    ) -> dict[str, Any]:
        if str(mode) != "paper":
            return {
                "ok": False,
                "existing": False,
                "run": None,
                "error_code": "paper_mode_required",
            }

        existing = self.store.get_execution_by_idempotency(idempotency_key)
        if existing:
            if idempotency_scope_conflict(
                existing,
                opportunity_id=opportunity_id,
                route_id=route_id,
                mode="paper",
                trade_amount_krw=trade_amount_krw,
            ):
                return idempotency_conflict_response(existing)
            return existing_run_response(existing, non_ok_statuses=TERMINAL_FAILURE_STATUSES)

        route = self.store.get_route(route_id)
        if not route:
            return {"ok": False, "existing": False, "run": None, "error_code": "route_not_found"}
        opportunity = self.store.get_opportunity(opportunity_id)
        if not opportunity:
            return {"ok": False, "existing": False, "run": None, "error_code": "opportunity_not_found"}
        if int(route.get("opportunity_id") or 0) != int(opportunity_id):
            return {
                "ok": False,
                "existing": False,
                "run": None,
                "error_code": "route_opportunity_mismatch",
            }

        route_type = str(route.get("route_type") or "same_dex_sell")
        policy = str(execution_policy or "").strip()
        buy_then_hold = policy == "buy_then_hold"
        route_steps = ["precheck", "dex_buy", "wallet_hold"] if buy_then_hold else ROUTE_STEPS.get(route_type, ROUTE_STEPS["same_dex_sell"])
        run = self.store.insert_execution_run(
            execution_key=f"paper:{uuid.uuid4().hex}",
            idempotency_key=idempotency_key,
            opportunity_id=opportunity_id,
            route_id=route_id,
            mode="paper",
            status="ENTERING",
            requested_by=requested_by,
            payload={
                "route_type": route_type,
                "trade_amount_krw": trade_amount_krw,
                "paper_only": True,
                "no_external_submission": True,
                "execution_policy": policy,
                "stop_after_buy": buy_then_hold,
            },
        )
        if not run.get("created", True):
            return existing_run_response(run, non_ok_statuses=TERMINAL_FAILURE_STATUSES)
        for step_key in route_steps:
            self.store.insert_execution_step(run_id=run["id"], step_key=step_key, status="PENDING")

        self._append_log(
            run=run,
            message="paper execution started",
            payload={"status": "ENTERING", "route_type": route_type, "mode": "paper", "execution_policy": policy},
        )
        advanced = self.advance_run(
            int(run["id"]),
            simulated_outcomes=simulated_outcomes,
            trade_amount_krw=trade_amount_krw,
        )
        final_run = advanced["run"]
        expected_status = "POSITION_OPEN" if buy_then_hold else "SETTLED"
        return {
            "ok": str(final_run.get("status")) == expected_status,
            "existing": False,
            "run": final_run,
        }

    def advance_run(
        self,
        run_id: int,
        *,
        simulated_outcomes: Mapping[str, str] | None = None,
        trade_amount_krw: float | None = None,
    ) -> dict[str, Any]:
        run = self.store.get_execution_run(run_id)
        if not run:
            raise ValueError("execution_run_not_found")
        if str(run.get("mode")) != "paper":
            raise ValueError("paper_mode_required")
        run_status = str(run.get("status") or "")
        if run_status in {*TERMINAL_SUCCESS_STATUSES, *TERMINAL_FAILURE_STATUSES}:
            return {"run": run, "completed": run_status in TERMINAL_SUCCESS_STATUSES, "terminal": True}

        route = self.store.get_route(int(run["route_id"])) or {}
        opportunity = self.store.get_opportunity(int(run["opportunity_id"])) or {}
        outcomes = {str(key): str(value).lower() for key, value in dict(simulated_outcomes or {}).items()}
        current_ms = int(run["started_at_ms"])

        for step in self.store.fetch_execution_steps(run_id):
            step_key = str(step["step_key"])
            if str(step.get("status")) == "COMPLETED":
                current_ms = max(current_ms, int(step.get("completed_at_ms") or current_ms))
                continue
            if str(step.get("status")) == "RECONCILE":
                return {"run": run, "completed": False}

            duration_ms = int(PAPER_STEP_DURATIONS_MS.get(step_key, 100))
            started_at_ms = current_ms
            completed_at_ms = started_at_ms + duration_ms
            self._start_step(run, step_key, started_at_ms=started_at_ms, duration_ms=duration_ms)

            if outcomes.get(step_key) == "unknown":
                reconciled = self._mark_simulated_unknown(
                    run=run,
                    step_key=step_key,
                    started_at_ms=started_at_ms,
                    completed_at_ms=completed_at_ms,
                    duration_ms=duration_ms,
                )
                return {"run": reconciled["run"], "step": reconciled["step"], "completed": False}

            self._complete_step(
                run=run,
                step_key=step_key,
                started_at_ms=started_at_ms,
                completed_at_ms=completed_at_ms,
                duration_ms=duration_ms,
            )
            self._apply_success_side_effects(
                run=run,
                opportunity=opportunity,
                route=route,
                step_key=step_key,
                observed_at_ms=completed_at_ms,
                trade_amount_krw=trade_amount_krw,
            )
            current_ms = completed_at_ms

        buy_then_hold = _run_execution_policy(run) == "buy_then_hold"
        final_status = "POSITION_OPEN" if buy_then_hold else "SETTLED"
        settled = self.store.update_execution_run(run_id, status=final_status)
        self._append_log(
            run=settled,
            message="paper execution wallet hold" if buy_then_hold else "paper execution settled",
            payload={"status": final_status, "mode": "paper", "execution_policy": _run_execution_policy(run)},
        )
        return {"run": settled, "completed": True}

    def _start_step(self, run: Mapping[str, Any], step_key: str, *, started_at_ms: int, duration_ms: int) -> None:
        route_type = _run_route_type(run)
        self.store.update_execution_step(
            run_id=int(run["id"]),
            step_key=step_key,
            status="RUNNING",
            started_at_ms=started_at_ms,
            duration_ms=duration_ms,
            payload={"mode": "paper", "transition": "started"},
        )
        self.store.append_event(
            event_type="execution.step.started",
            opportunity_id=int(run["opportunity_id"]),
            route_id=int(run["route_id"]),
            run_id=int(run["id"]),
            payload={"step_key": step_key, "status": "RUNNING", "started_at_ms": started_at_ms},
        )
        self._append_flow_updates(
            run=run,
            route_type=route_type,
            step_key=step_key,
            status="RUNNING",
            started_at_ms=started_at_ms,
            duration_ms=duration_ms,
        )
        self._append_log(
            run=run,
            message=f"paper step started: {step_key}",
            payload={"step_key": step_key, "status": "RUNNING"},
        )

    def _complete_step(
        self,
        *,
        run: Mapping[str, Any],
        step_key: str,
        started_at_ms: int,
        completed_at_ms: int,
        duration_ms: int,
    ) -> None:
        route_type = _run_route_type(run)
        self.store.update_execution_step(
            run_id=int(run["id"]),
            step_key=step_key,
            status="COMPLETED",
            started_at_ms=started_at_ms,
            completed_at_ms=completed_at_ms,
            duration_ms=duration_ms,
            payload={"mode": "paper", "transition": "completed"},
        )
        self.store.append_event(
            event_type="execution.step.completed",
            opportunity_id=int(run["opportunity_id"]),
            route_id=int(run["route_id"]),
            run_id=int(run["id"]),
            payload={
                "step_key": step_key,
                "status": "COMPLETED",
                "started_at_ms": started_at_ms,
                "completed_at_ms": completed_at_ms,
                "duration_ms": duration_ms,
            },
        )
        self._append_flow_updates(
            run=run,
            route_type=route_type,
            step_key=step_key,
            status="COMPLETED",
            started_at_ms=started_at_ms,
            completed_at_ms=completed_at_ms,
            duration_ms=duration_ms,
        )
        self._append_log(
            run=run,
            message=f"paper step completed: {step_key}",
            payload={"step_key": step_key, "status": "COMPLETED", "duration_ms": duration_ms},
        )

    def _mark_simulated_unknown(
        self,
        *,
        run: Mapping[str, Any],
        step_key: str,
        started_at_ms: int,
        completed_at_ms: int,
        duration_ms: int,
    ) -> dict[str, Any]:
        error_code = "paper_outcome_unknown"
        step = self.store.update_execution_step(
            run_id=int(run["id"]),
            step_key=step_key,
            status="RECONCILE",
            error_code=error_code,
            error_msg="simulated paper outcome is unknown",
            started_at_ms=started_at_ms,
            completed_at_ms=completed_at_ms,
            duration_ms=duration_ms,
            payload={"mode": "paper", "transition": "reconcile", "outcome": "unknown"},
        )
        updated = self.store.update_execution_run(
            int(run["id"]),
            status="MANUAL_REVIEW",
            error_code=error_code,
            error_msg=f"manual review required for paper step {step_key}",
        )
        self.store.append_dead_letter(
            reason="paper_unknown_outcome",
            deadletter_key=f"paper_unknown_outcome:{run['id']}:{step_key}",
            error_code=error_code,
            retryable=False,
            payload={"run_id": int(run["id"]), "step_key": step_key, "mode": "paper"},
        )
        self.store.append_event(
            event_type="execution.step.reconcile",
            opportunity_id=int(run["opportunity_id"]),
            route_id=int(run["route_id"]),
            run_id=int(run["id"]),
            severity="warning",
            payload={"step_key": step_key, "status": "RECONCILE", "error_code": error_code},
        )
        self._append_flow_updates(
            run=updated,
            route_type=_run_route_type(run),
            step_key=step_key,
            status="RECONCILE",
            started_at_ms=started_at_ms,
            completed_at_ms=completed_at_ms,
            duration_ms=duration_ms,
            severity="warning",
            extra_payload={"error_code": error_code},
        )
        self._append_log(
            run=updated,
            message=f"paper step requires manual review: {step_key}",
            severity="warning",
            payload={"step_key": step_key, "status": "RECONCILE", "error_code": error_code},
        )
        return {"run": updated, "step": step}

    def _append_flow_updates(
        self,
        *,
        run: Mapping[str, Any],
        route_type: str,
        step_key: str,
        status: str,
        started_at_ms: int | None = None,
        completed_at_ms: int | None = None,
        duration_ms: int | None = None,
        severity: str = "info",
        extra_payload: Mapping[str, Any] | None = None,
    ) -> None:
        state = ui_state_for_step_status(status)
        base_payload = {
            "route_type": route_type,
            "step_key": step_key,
            "status": status,
            "state": state,
            "started_at_ms": started_at_ms,
            "completed_at_ms": completed_at_ms,
            "duration_ms": duration_ms,
            **dict(extra_payload or {}),
        }
        node_id = step_node_id(route_type, step_key)
        if node_id:
            self.store.append_event(
                event_type="flow.node.update",
                opportunity_id=int(run["opportunity_id"]),
                route_id=int(run["route_id"]),
                run_id=int(run["id"]),
                severity=severity,
                payload={**base_payload, "node_id": node_id},
            )
        edge_id = step_edge_id(route_type, step_key)
        if edge_id:
            self.store.append_event(
                event_type="flow.edge.update",
                opportunity_id=int(run["opportunity_id"]),
                route_id=int(run["route_id"]),
                run_id=int(run["id"]),
                severity=severity,
                payload={**base_payload, "edge_id": edge_id},
            )

    def _apply_success_side_effects(
        self,
        *,
        run: Mapping[str, Any],
        opportunity: Mapping[str, Any],
        route: Mapping[str, Any],
        step_key: str,
        observed_at_ms: int,
        trade_amount_krw: float | None,
    ) -> None:
        if step_key == "dex_buy":
            self.store.update_execution_run(int(run["id"]), status="POSITION_OPEN")
            position = self._upsert_position(
                run=run,
                opportunity=opportunity,
                route=route,
                status="OPEN",
                observed_at_ms=observed_at_ms,
                trade_amount_krw=trade_amount_krw,
                closed=False,
            )
            self._append_position_update(run, position, status="OPEN")
        elif step_key == "exit_route_select":
            self.store.update_execution_run(int(run["id"]), status="EXITING")
        elif step_key in EXIT_STEPS:
            position = self._upsert_position(
                run=run,
                opportunity=opportunity,
                route=route,
                status="EXITING",
                observed_at_ms=observed_at_ms,
                trade_amount_krw=trade_amount_krw,
                closed=False,
            )
            self._append_position_update(run, position, status="EXITING")
        elif step_key == "settle":
            position = self._upsert_position(
                run=run,
                opportunity=opportunity,
                route=route,
                status="SETTLED",
                observed_at_ms=observed_at_ms,
                trade_amount_krw=trade_amount_krw,
                closed=True,
            )
            self._append_position_update(run, position, status="SETTLED")

    def _upsert_position(
        self,
        *,
        run: Mapping[str, Any],
        opportunity: Mapping[str, Any],
        route: Mapping[str, Any],
        status: str,
        observed_at_ms: int,
        trade_amount_krw: float | None,
        closed: bool,
    ) -> dict[str, Any]:
        values = self._position_values(route=route, run=run, trade_amount_krw=trade_amount_krw)
        position = self.store.upsert_position(
            position_key=f"paper:{run['id']}",
            opportunity_id=int(run["opportunity_id"]),
            run_id=int(run["id"]),
            asset_id=int(opportunity["asset_id"]),
            status=status,
            qty_raw=str(values["qty_raw"]),
            avg_buy_price_krw=float(values["avg_buy_price_krw"]),
            realized_pnl_krw=float(values["pnl_placeholder_krw"]) if closed else 0.0,
            opened_at_ms=int(run["started_at_ms"]),
            closed_at_ms=observed_at_ms if closed else None,
            payload={
                "mode": "paper",
                "route_type": route.get("route_type"),
                "current_status": status,
                "live_exit_estimate_krw": values["live_exit_estimate_krw"],
                "pnl_placeholder_krw": values["pnl_placeholder_krw"],
                "paper_only": True,
            },
        )
        self.store.insert_position_mark(
            position_id=int(position["id"]),
            observed_at_ms=observed_at_ms,
            mark_price_krw=float(values["live_exit_estimate_krw"] if status != "OPEN" else values["avg_buy_price_krw"]),
            unrealized_pnl_krw=float(values["pnl_placeholder_krw"] if status != "OPEN" else 0.0),
            route_status={
                "route_id": int(run["route_id"]),
                "run_id": int(run["id"]),
                "step_status": status,
                "mode": "paper",
            },
        )
        return position

    def _position_values(
        self,
        *,
        route: Mapping[str, Any],
        run: Mapping[str, Any],
        trade_amount_krw: float | None,
    ) -> dict[str, Any]:
        quote = self.store.get_latest_route_quote(int(run["route_id"]))
        run_payload = run.get("payload") if isinstance(run.get("payload"), Mapping) else {}
        notional = _first_positive_float(
            trade_amount_krw,
            run_payload.get("trade_amount_krw") if isinstance(run_payload, Mapping) else None,
            (quote or {}).get("amount_in_value_krw"),
            100_000.0,
        )
        live_exit = _first_positive_float(
            (quote or {}).get("amount_out_expected_krw"),
            (quote or {}).get("amount_out_min_krw"),
            notional * (1.0 + (float(route.get("edge_worst_bps") or 0.0) / 10_000.0)),
        )
        qty_raw = str((quote or {}).get("amount_in_raw") or "1")
        pnl = live_exit - notional
        return {
            "qty_raw": qty_raw,
            "avg_buy_price_krw": notional,
            "live_exit_estimate_krw": live_exit,
            "pnl_placeholder_krw": pnl,
        }

    def _append_log(
        self,
        *,
        run: Mapping[str, Any],
        message: str,
        payload: Mapping[str, Any] | None = None,
        severity: str = "info",
    ) -> None:
        merged = {"message": message, "mode": "paper", **dict(payload or {})}
        self.store.append_event(
            event_type="execution.log.append",
            opportunity_id=int(run["opportunity_id"]),
            route_id=int(run["route_id"]),
            run_id=int(run["id"]),
            severity=severity,
            payload=merged,
        )

    def _append_position_update(self, run: Mapping[str, Any], position: Mapping[str, Any], *, status: str) -> None:
        self.store.append_event(
            event_type="position.update",
            opportunity_id=int(run["opportunity_id"]),
            route_id=int(run["route_id"]),
            run_id=int(run["id"]),
            payload={
                "position_id": int(position["id"]),
                "status": status,
                "mode": "paper",
            },
        )


def _first_positive_float(*values: Any) -> float:
    for value in values:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return 0.0


def _run_route_type(run: Mapping[str, Any]) -> str:
    payload = run.get("payload") if isinstance(run.get("payload"), Mapping) else {}
    return str(payload.get("route_type") or "same_dex_sell")


def _run_execution_policy(run: Mapping[str, Any]) -> str:
    payload = run.get("payload") if isinstance(run.get("payload"), Mapping) else {}
    return str(payload.get("execution_policy") or "")
