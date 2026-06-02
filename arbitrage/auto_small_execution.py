from __future__ import annotations

import uuid
from collections.abc import Mapping
from typing import Any

from .dex_submit import DexSwapRequest, DexSwapSubmitAdapter, DryRunDexSwapAdapter
from .execution_safety import existing_run_response, idempotency_conflict_response, idempotency_scope_conflict
from .execution_flow import step_edge_id, step_node_id, ui_state_for_step_status
from .paper_execution import EXIT_STEPS, PAPER_STEP_DURATIONS_MS, ROUTE_STEPS
from .store import ArbitrageStore


AUTO_SMALL_ROUTE_TYPE = "same_dex_sell"
DEX_SUBMIT_STEPS = {"dex_buy", "same_dex_sell"}
NON_OK_EXISTING_RUN_STATUSES = {"ABORTED", "BLOCKED", "FAILED", "MANUAL_REVIEW"}
RECONCILE_ADAPTER_STATUSES = {"unknown", "pending", "timeout", "reconcile"}


class AutoSmallSameDexDryRunRunner:
    """Part 7 auto_small runner for same-chain DEX dry-runs only."""

    def __init__(self, store: ArbitrageStore, adapter: DexSwapSubmitAdapter | None = None):
        self.store = store
        self.adapter = adapter or DryRunDexSwapAdapter()

    def start(
        self,
        *,
        opportunity_id: int,
        route_id: int,
        mode: str,
        idempotency_key: str,
        requested_by: str = "system",
        trade_amount_krw: float | None = None,
    ) -> dict[str, Any]:
        if str(mode) != "auto_small":
            return {
                "ok": False,
                "existing": False,
                "run": None,
                "error_code": "auto_small_mode_required",
            }

        existing = self.store.get_execution_by_idempotency(idempotency_key)
        if existing:
            if idempotency_scope_conflict(
                existing,
                opportunity_id=opportunity_id,
                route_id=route_id,
                mode="auto_small",
                trade_amount_krw=trade_amount_krw,
            ):
                return idempotency_conflict_response(existing)
            return existing_run_response(existing, non_ok_statuses=NON_OK_EXISTING_RUN_STATUSES)

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
        if str(route.get("route_type") or "") != AUTO_SMALL_ROUTE_TYPE:
            return {
                "ok": False,
                "existing": False,
                "run": None,
                "error_code": "route_type_not_supported",
            }

        amount_krw = _first_positive_float(trade_amount_krw)
        run = self.store.insert_execution_run(
            execution_key=f"auto_small:{uuid.uuid4().hex}",
            idempotency_key=idempotency_key,
            opportunity_id=opportunity_id,
            route_id=route_id,
            mode="auto_small",
            status="ENTERING",
            requested_by=requested_by,
            payload={
                "route_type": AUTO_SMALL_ROUTE_TYPE,
                "trade_amount_krw": amount_krw,
                "dry_run": True,
                "dry_run_only": True,
                "same_chain_dex_only": True,
                "adapter_name": self.adapter.adapter_name,
                "adapter_capabilities": list(self.adapter.capabilities),
                "no_real_submission": True,
                "no_external_provider_call": True,
                "no_signed_payload": True,
            },
        )
        if not run.get("created", True):
            return existing_run_response(run, non_ok_statuses=NON_OK_EXISTING_RUN_STATUSES)
        for step_key in ROUTE_STEPS[AUTO_SMALL_ROUTE_TYPE]:
            self.store.insert_execution_step(
                run_id=run["id"],
                step_key=step_key,
                status="PENDING",
                payload={"mode": "auto_small", "dry_run": True},
            )

        self._append_log(
            run=run,
            message="auto_small dry-run started",
            payload={"status": "ENTERING", "run_status": "ENTERING", "route_type": AUTO_SMALL_ROUTE_TYPE},
        )
        advanced = self.advance_run(int(run["id"]), trade_amount_krw=trade_amount_krw)
        final_run = advanced["run"]
        return {
            "ok": str(final_run.get("status")) == "SETTLED",
            "existing": False,
            "run": final_run,
        }

    def advance_run(self, run_id: int, *, trade_amount_krw: float | None = None) -> dict[str, Any]:
        run = self.store.get_execution_run(run_id)
        if not run:
            raise ValueError("execution_run_not_found")
        if str(run.get("mode")) != "auto_small":
            raise ValueError("auto_small_mode_required")

        route = self.store.get_route(int(run["route_id"])) or {}
        opportunity = self.store.get_opportunity(int(run["opportunity_id"])) or {}
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

            submit_context: dict[str, Any] = {}
            if step_key in DEX_SUBMIT_STEPS:
                submit_context = self._execute_dry_run_swap_step(
                    run=run,
                    route=route,
                    step=step,
                    step_key=step_key,
                    amount_krw=trade_amount_krw,
                    started_at_ms=started_at_ms,
                    completed_at_ms=completed_at_ms,
                    duration_ms=duration_ms,
                )
                if not submit_context.get("ok"):
                    return {"run": submit_context["run"], "step": submit_context["step"], "completed": False}

            self._complete_step(
                run=run,
                step_key=step_key,
                started_at_ms=started_at_ms,
                completed_at_ms=completed_at_ms,
                duration_ms=duration_ms,
                extra_payload=submit_context.get("payload"),
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

        settled = self.store.update_execution_run(run_id, status="SETTLED")
        self._append_log(
            run=settled,
            message="auto_small dry-run settled",
            payload={"status": "SETTLED", "run_status": "SETTLED"},
        )
        return {"run": settled, "completed": True}

    def _execute_dry_run_swap_step(
        self,
        *,
        run: Mapping[str, Any],
        route: Mapping[str, Any],
        step: Mapping[str, Any],
        step_key: str,
        amount_krw: float | None,
        started_at_ms: int,
        completed_at_ms: int,
        duration_ms: int,
    ) -> dict[str, Any]:
        request = self._dex_request(run=run, route=route, step_key=step_key, amount_krw=amount_krw)
        try:
            quote = self.adapter.quote(request)
            if not _adapter_success(quote.status):
                return self._mark_adapter_not_success(
                    run=run,
                    step_key=step_key,
                    phase="quote",
                    adapter_status=quote.status,
                    evidence=quote.to_dict(),
                    external_ref="",
                    started_at_ms=started_at_ms,
                    completed_at_ms=completed_at_ms,
                    duration_ms=duration_ms,
                )
            build = self.adapter.build(request, quote)
            if not _adapter_success(build.status):
                return self._mark_adapter_not_success(
                    run=run,
                    step_key=step_key,
                    phase="build",
                    adapter_status=build.status,
                    evidence=build.to_dict(),
                    external_ref=build.build_ref,
                    started_at_ms=started_at_ms,
                    completed_at_ms=completed_at_ms,
                    duration_ms=duration_ms,
                )
            submit_result = self.adapter.submit(request, build)
            _validate_submit_result(submit_result)
            self.store.record_dry_run_transaction(
                chain_id=request.chain,
                tx_hash=submit_result.tx_hash,
                run_id=int(run["id"]),
                step_id=int(step["id"]),
                tx_type=f"{step_key}_dex_swap",
                adapter_name=submit_result.adapter_name,
                submit_ref=submit_result.submit_ref,
                status=f"DRY_RUN_{submit_result.status}",
                payload={
                    **submit_result.to_dict(),
                    "mode": "auto_small",
                    "dry_run_only": True,
                    "step_key": step_key,
                },
            )
            if not _adapter_success(submit_result.status):
                return self._mark_adapter_not_success(
                    run=run,
                    step_key=step_key,
                    phase="submit",
                    adapter_status=submit_result.status,
                    evidence=submit_result.to_dict(),
                    external_ref=submit_result.tx_hash or submit_result.submit_ref,
                    started_at_ms=started_at_ms,
                    completed_at_ms=completed_at_ms,
                    duration_ms=duration_ms,
                )
            status = self.adapter.status(request, submit_result)
            if not _adapter_success(status.status):
                return self._mark_adapter_not_success(
                    run=run,
                    step_key=step_key,
                    phase="status",
                    adapter_status=status.status,
                    evidence=status.to_dict(),
                    external_ref=status.tx_hash or status.submit_ref,
                    started_at_ms=started_at_ms,
                    completed_at_ms=completed_at_ms,
                    duration_ms=duration_ms,
                )
        except Exception as exc:
            return self._mark_adapter_error(
                run=run,
                step_key=step_key,
                error_code="auto_small_adapter_error",
                error_msg=str(exc),
                started_at_ms=started_at_ms,
                completed_at_ms=completed_at_ms,
                duration_ms=duration_ms,
            )

        return {
            "ok": True,
            "payload": {
                "adapter_name": submit_result.adapter_name,
                "submit_ref": submit_result.submit_ref,
                "tx_hash": submit_result.tx_hash,
                "dry_run": True,
                "quote_evidence": submit_result.quote_evidence,
                "build_evidence": submit_result.build_evidence,
                "payload_evidence": submit_result.payload_evidence,
                "status_evidence": status.evidence,
            },
        }

    def _dex_request(
        self,
        *,
        run: Mapping[str, Any],
        route: Mapping[str, Any],
        step_key: str,
        amount_krw: float | None,
    ) -> DexSwapRequest:
        buy_market = self.store.get_market_detail(int(route.get("buy_market_id") or 0)) or {}
        sell_market = self.store.get_market_detail(int(route.get("sell_market_id") or 0)) or {}
        route_payload = route.get("payload") if isinstance(route.get("payload"), Mapping) else {}
        run_payload = run.get("payload") if isinstance(run.get("payload"), Mapping) else {}
        pool_ca = (
            buy_market.get("pool_ca")
            if step_key == "dex_buy"
            else sell_market.get("pool_ca") or route_payload.get("sell_pool_ca") or buy_market.get("pool_ca")
        )
        trade_amount = _first_positive_float(
            amount_krw,
            run_payload.get("trade_amount_krw") if isinstance(run_payload, Mapping) else None,
            (self.store.get_latest_route_quote(int(run["route_id"])) or {}).get("amount_in_value_krw"),
            100_000.0,
        )
        slippage_bps = int(
            _first_positive_float(
                route_payload.get("slippage_bps") if isinstance(route_payload, Mapping) else None,
                route_payload.get("max_slippage_bps") if isinstance(route_payload, Mapping) else None,
                150,
            )
        )
        return DexSwapRequest(
            route_id=int(run["route_id"]),
            opportunity_id=int(run["opportunity_id"]),
            chain=str(buy_market.get("chain") or sell_market.get("chain") or "").upper(),
            buy_market=buy_market,
            sell_market=sell_market,
            token_ca=str(buy_market.get("token_ca") or sell_market.get("token_ca") or route_payload.get("token_ca") or ""),
            pool_ca=str(pool_ca or ""),
            amount_krw=trade_amount,
            slippage_bps=slippage_bps,
            idempotency_key=f"{run['id']}:{step_key}:{run['idempotency_key']}",
            step_key=step_key,
            payload={
                "mode": "auto_small",
                "dry_run": True,
                "dry_run_only": True,
                "adapter_capabilities": list(self.adapter.capabilities),
                "route_payload": dict(route_payload),
                "dry_run_simulation": route_payload.get("dry_run_simulation") if isinstance(route_payload, Mapping) else {},
            },
        )

    def _start_step(self, run: Mapping[str, Any], step_key: str, *, started_at_ms: int, duration_ms: int) -> None:
        self.store.update_execution_step(
            run_id=int(run["id"]),
            step_key=step_key,
            status="RUNNING",
            started_at_ms=started_at_ms,
            duration_ms=duration_ms,
            payload={"mode": "auto_small", "dry_run": True, "transition": "started"},
        )
        self.store.append_event(
            event_type="execution.step.started",
            opportunity_id=int(run["opportunity_id"]),
            route_id=int(run["route_id"]),
            run_id=int(run["id"]),
            payload={"step_key": step_key, "status": "RUNNING", "started_at_ms": started_at_ms, "mode": "auto_small"},
        )
        self._append_flow_updates(
            run=run,
            step_key=step_key,
            status="RUNNING",
            started_at_ms=started_at_ms,
            duration_ms=duration_ms,
        )
        self._append_log(
            run=run,
            message=f"auto_small step started: {step_key}",
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
        extra_payload: Mapping[str, Any] | None = None,
    ) -> None:
        payload = {
            "mode": "auto_small",
            "dry_run": True,
            "transition": "completed",
            **dict(extra_payload or {}),
        }
        self.store.update_execution_step(
            run_id=int(run["id"]),
            step_key=step_key,
            status="COMPLETED",
            external_ref=str(payload.get("tx_hash") or payload.get("submit_ref") or ""),
            started_at_ms=started_at_ms,
            completed_at_ms=completed_at_ms,
            duration_ms=duration_ms,
            payload=payload,
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
                "mode": "auto_small",
                "dry_run": True,
                "tx_hash": payload.get("tx_hash"),
                "submit_ref": payload.get("submit_ref"),
            },
        )
        self._append_flow_updates(
            run=run,
            step_key=step_key,
            status="COMPLETED",
            started_at_ms=started_at_ms,
            completed_at_ms=completed_at_ms,
            duration_ms=duration_ms,
            extra_payload={"tx_hash": payload.get("tx_hash"), "submit_ref": payload.get("submit_ref")},
        )
        self._append_log(
            run=run,
            message=f"auto_small step completed: {step_key}",
            payload={
                "step_key": step_key,
                "status": "COMPLETED",
                "duration_ms": duration_ms,
                "tx_hash": payload.get("tx_hash"),
                "submit_ref": payload.get("submit_ref"),
            },
        )

    def _mark_adapter_not_success(
        self,
        *,
        run: Mapping[str, Any],
        step_key: str,
        phase: str,
        adapter_status: str,
        evidence: Mapping[str, Any],
        external_ref: str,
        started_at_ms: int,
        completed_at_ms: int,
        duration_ms: int,
    ) -> dict[str, Any]:
        status_key = str(adapter_status or "unknown").strip().lower()
        reconcile = status_key in RECONCILE_ADAPTER_STATUSES
        step_status = "RECONCILE" if reconcile else "FAILED"
        run_status = "MANUAL_REVIEW" if reconcile else "FAILED"
        error_code = f"auto_small_adapter_{phase}_{status_key or 'not_success'}"
        refs = _adapter_refs(evidence=evidence, external_ref=external_ref)
        step = self.store.update_execution_step(
            run_id=int(run["id"]),
            step_key=step_key,
            status=step_status,
            external_ref=refs["external_ref"],
            error_code=error_code,
            error_msg=f"adapter {phase} status was {adapter_status}",
            started_at_ms=started_at_ms,
            completed_at_ms=completed_at_ms,
            duration_ms=duration_ms,
            payload={
                "mode": "auto_small",
                "dry_run": True,
                "transition": "reconcile" if reconcile else "failed",
                "phase": phase,
                "adapter_status": adapter_status,
                "evidence": dict(evidence),
            },
        )
        updated = self.store.update_execution_run(
            int(run["id"]),
            status=run_status,
            error_code=error_code,
            error_msg=f"auto_small dry-run stopped at {step_key}",
        )
        self.store.append_dead_letter(
            reason="auto_small_reconcile_required" if reconcile else "auto_small_adapter_not_success",
            deadletter_key=f"auto_small_adapter:{run['id']}:{step_key}:{phase}",
            error_code=error_code,
            retryable=False,
            payload={
                "run_id": int(run["id"]),
                "route_id": int(run["route_id"]),
                "step_key": step_key,
                "phase": phase,
                "adapter_status": adapter_status,
                "external_ref": refs["external_ref"],
                "tx_hash": refs["tx_hash"],
                "submit_ref": refs["submit_ref"],
            },
        )
        self.store.append_event(
            event_type="execution.step.reconcile" if reconcile else "error",
            opportunity_id=int(run["opportunity_id"]),
            route_id=int(run["route_id"]),
            run_id=int(run["id"]),
            severity="warning" if reconcile else "error",
            payload={
                "run_id": int(run["id"]),
                "route_id": int(run["route_id"]),
                "step_key": step_key,
                "status": step_status,
                "run_status": run_status,
                "mode": "auto_small",
                "dry_run": True,
                "dry_run_only": True,
                "phase": phase,
                "error_code": error_code,
                "adapter_status": adapter_status,
                "external_ref": refs["external_ref"],
                "tx_hash": refs["tx_hash"],
                "submit_ref": refs["submit_ref"],
            },
        )
        self._append_flow_updates(
            run=updated,
            step_key=step_key,
            status=step_status,
            started_at_ms=started_at_ms,
            completed_at_ms=completed_at_ms,
            duration_ms=duration_ms,
            severity="warning" if reconcile else "error",
            extra_payload={
                "error_code": error_code,
                "adapter_status": adapter_status,
                "external_ref": refs["external_ref"],
                "tx_hash": refs["tx_hash"],
                "submit_ref": refs["submit_ref"],
            },
        )
        self._append_log(
            run=updated,
            message=f"auto_small dry-run stopped: {step_key}",
            severity="warning" if reconcile else "error",
            payload={"step_key": step_key, "status": step_status, "run_status": run_status, "error_code": error_code},
        )
        return {"ok": False, "run": updated, "step": step}

    def _mark_adapter_error(
        self,
        *,
        run: Mapping[str, Any],
        step_key: str,
        error_code: str,
        error_msg: str,
        started_at_ms: int,
        completed_at_ms: int,
        duration_ms: int,
    ) -> dict[str, Any]:
        step = self.store.update_execution_step(
            run_id=int(run["id"]),
            step_key=step_key,
            status="FAILED",
            error_code=error_code,
            error_msg=error_msg,
            started_at_ms=started_at_ms,
            completed_at_ms=completed_at_ms,
            duration_ms=duration_ms,
            payload={"mode": "auto_small", "dry_run": True, "transition": "failed", "error_msg": error_msg},
        )
        updated = self.store.update_execution_run(int(run["id"]), status="FAILED", error_code=error_code, error_msg=error_msg)
        self.store.append_dead_letter(
            reason="auto_small_adapter_error",
            deadletter_key=f"auto_small_adapter_error:{run['id']}:{step_key}",
            error_code=error_code,
            retryable=False,
            payload={"run_id": int(run["id"]), "route_id": int(run["route_id"]), "step_key": step_key, "error_msg": error_msg},
        )
        self.store.append_event(
            event_type="error",
            opportunity_id=int(run["opportunity_id"]),
            route_id=int(run["route_id"]),
            run_id=int(run["id"]),
            severity="error",
            payload={
                "step_key": step_key,
                "status": "FAILED",
                "run_status": "FAILED",
                "mode": "auto_small",
                "dry_run": True,
                "dry_run_only": True,
                "error_code": error_code,
            },
        )
        self._append_flow_updates(
            run=updated,
            step_key=step_key,
            status="FAILED",
            started_at_ms=started_at_ms,
            completed_at_ms=completed_at_ms,
            duration_ms=duration_ms,
            severity="error",
            extra_payload={"error_code": error_code},
        )
        self._append_log(
            run=updated,
            message=f"auto_small dry-run failed: {step_key}",
            severity="error",
            payload={"step_key": step_key, "status": "FAILED", "run_status": "FAILED", "error_code": error_code},
        )
        return {"ok": False, "run": updated, "step": step}

    def _append_flow_updates(
        self,
        *,
        run: Mapping[str, Any],
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
            "route_type": AUTO_SMALL_ROUTE_TYPE,
            "step_key": step_key,
            "status": status,
            "state": state,
            "started_at_ms": started_at_ms,
            "completed_at_ms": completed_at_ms,
            "duration_ms": duration_ms,
            "mode": "auto_small",
            "dry_run": True,
            **dict(extra_payload or {}),
        }
        node_id = step_node_id(AUTO_SMALL_ROUTE_TYPE, step_key)
        if node_id:
            self.store.append_event(
                event_type="flow.node.update",
                opportunity_id=int(run["opportunity_id"]),
                route_id=int(run["route_id"]),
                run_id=int(run["id"]),
                severity=severity,
                payload={**base_payload, "node_id": node_id},
            )
        edge_id = step_edge_id(AUTO_SMALL_ROUTE_TYPE, step_key)
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
            position_key=f"auto_small_dry_run:{run['id']}",
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
                "mode": "auto_small",
                "route_type": route.get("route_type"),
                "current_status": status,
                "live_exit_estimate_krw": values["live_exit_estimate_krw"],
                "pnl_placeholder_krw": values["pnl_placeholder_krw"],
                "dry_run": True,
                "dry_run_only": True,
                "not_live_trading": True,
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
                "mode": "auto_small",
                "dry_run": True,
                "not_live_trading": True,
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
        merged = {"message": message, "mode": "auto_small", "dry_run": True, **dict(payload or {})}
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
                "mode": "auto_small",
                "dry_run": True,
                "not_live_trading": True,
            },
        )


def _adapter_success(status: Any) -> bool:
    return str(status or "").strip().lower() == "success"


def _validate_submit_result(result: Any) -> None:
    if not bool(getattr(result, "dry_run", False)):
        raise ValueError("dry_run_submit_result_required")
    if not str(getattr(result, "adapter_name", "") or "").strip():
        raise ValueError("adapter_name_required")
    if not str(getattr(result, "status", "") or "").strip():
        raise ValueError("adapter_status_required")
    if not str(getattr(result, "tx_hash", "") or "").strip():
        raise ValueError("adapter_tx_hash_required")
    if not str(getattr(result, "submit_ref", "") or "").strip():
        raise ValueError("adapter_submit_ref_required")


def _adapter_refs(*, evidence: Mapping[str, Any], external_ref: str) -> dict[str, str]:
    tx_hash = str(evidence.get("tx_hash") or "")
    submit_ref = str(evidence.get("submit_ref") or "")
    payload_evidence = evidence.get("payload_evidence")
    if isinstance(payload_evidence, Mapping):
        tx_hash = tx_hash or str(payload_evidence.get("tx_hash") or "")
        submit_ref = submit_ref or str(payload_evidence.get("submit_ref") or "")
    resolved_external_ref = str(external_ref or tx_hash or submit_ref)
    return {
        "external_ref": resolved_external_ref,
        "tx_hash": tx_hash,
        "submit_ref": submit_ref,
    }


def _first_positive_float(*values: Any) -> float:
    for value in values:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return 0.0
