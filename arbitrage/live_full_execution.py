from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from typing import Any

from .bridge_submit import BridgeSubmitAdapter, BridgeSubmitRequest, DeterministicBridgeSubmitAdapter
from .cex_trade import CexTradeAdapter, CexTradeRequest, DeterministicCexTradeAdapter
from .dex_submit import DexSwapRequest, DexSwapSubmitAdapter, DryRunDexSwapAdapter
from .execution_safety import existing_run_response, idempotency_conflict_response, idempotency_scope_conflict
from .execution_flow import step_edge_id, step_node_id, ui_state_for_step_status
from .paper_execution import EXIT_STEPS, PAPER_STEP_DURATIONS_MS, ROUTE_STEPS
from .store import ArbitrageStore


LIVE_FULL_ROUTE_TYPES = {"direct_cex_sell", "bridge_dex_sell", "bridge_cex_sell"}
DEX_STEPS = {"dex_buy", "bridge_dex_sell"}
BRIDGE_STEPS = {"bridge"}
CEX_DEPOSIT_STEPS = {"cex_deposit"}
CEX_ORDER_STEPS = {"cex_sell"}
NON_OK_EXISTING_RUN_STATUSES = {"ABORTED", "BLOCKED", "FAILED", "MANUAL_REVIEW"}
RECONCILE_ADAPTER_STATUSES = {"unknown", "pending", "timeout", "reconcile", "partial"}


class LiveFullBridgeCexRunner:
    """Part 8 live_full saga for bridge/CEX route types.

    The default adapters are deterministic and simulated. They preserve the
    live_full saga contract without submitting real bridge, DEX, or CEX orders.
    """

    def __init__(
        self,
        store: ArbitrageStore,
        *,
        dex_adapter: DexSwapSubmitAdapter | None = None,
        bridge_adapter: BridgeSubmitAdapter | None = None,
        cex_adapter: CexTradeAdapter | None = None,
        gate_checker: Callable[..., list[str]] | None = None,
    ):
        self.store = store
        self.dex_adapter = dex_adapter or DryRunDexSwapAdapter()
        self.bridge_adapter = bridge_adapter or DeterministicBridgeSubmitAdapter()
        self.cex_adapter = cex_adapter or DeterministicCexTradeAdapter()
        self.gate_checker = gate_checker

    def start(
        self,
        *,
        opportunity_id: int,
        route_id: int,
        mode: str,
        idempotency_key: str,
        requested_by: str = "system",
        trade_amount_krw: float | None = None,
        approval: Mapping[str, Any] | None = None,
        engine_gate_checked: bool = False,
    ) -> dict[str, Any]:
        if str(mode) != "live_full":
            return {"ok": False, "existing": False, "run": None, "error_code": "live_full_mode_required"}

        existing = self.store.get_execution_by_idempotency(idempotency_key)
        if existing:
            if idempotency_scope_conflict(
                existing,
                opportunity_id=opportunity_id,
                route_id=route_id,
                mode="live_full",
                trade_amount_krw=trade_amount_krw,
            ):
                return idempotency_conflict_response(existing)
            return existing_run_response(existing, non_ok_statuses=NON_OK_EXISTING_RUN_STATUSES)

        gate_blockers = self._gate_blockers(
            opportunity_id=opportunity_id,
            route_id=route_id,
            mode="live_full",
            trade_amount_krw=trade_amount_krw,
            engine_gate_checked=engine_gate_checked,
        )
        if gate_blockers:
            return {
                "ok": False,
                "existing": False,
                "run": None,
                "error_code": ",".join(gate_blockers),
                "blockers": gate_blockers,
            }

        route = self.store.get_route(route_id)
        if not route:
            return {"ok": False, "existing": False, "run": None, "error_code": "route_not_found"}
        opportunity = self.store.get_opportunity(opportunity_id)
        if not opportunity:
            return {"ok": False, "existing": False, "run": None, "error_code": "opportunity_not_found"}
        if int(route.get("opportunity_id") or 0) != int(opportunity_id):
            return {"ok": False, "existing": False, "run": None, "error_code": "route_opportunity_mismatch"}

        route_type = str(route.get("route_type") or "")
        if route_type not in LIVE_FULL_ROUTE_TYPES:
            return {"ok": False, "existing": False, "run": None, "error_code": "route_type_not_supported"}

        run = self.store.insert_execution_run(
            execution_key=f"live_full:{uuid.uuid4().hex}",
            idempotency_key=idempotency_key,
            opportunity_id=opportunity_id,
            route_id=route_id,
            mode="live_full",
            status="ENTERING",
            requested_by=requested_by,
            payload={
                "route_type": route_type,
                "trade_amount_krw": _first_positive_float(trade_amount_krw),
                "approval": dict(approval or {}),
                "engine_gate_checked": bool(engine_gate_checked),
                "dry_run": bool(getattr(self.bridge_adapter, "dry_run", False))
                and bool(getattr(self.cex_adapter, "dry_run", False))
                and bool(getattr(self.dex_adapter, "dry_run", False)),
                "simulated": bool(getattr(self.bridge_adapter, "simulated", True))
                and bool(getattr(self.cex_adapter, "simulated", True)),
                "adapter_boundary": "deterministic_default_or_explicit_adapter",
                "dex_adapter_name": self.dex_adapter.adapter_name,
                "bridge_adapter_name": self.bridge_adapter.adapter_name,
                "cex_adapter_name": self.cex_adapter.adapter_name,
                "dex_adapter_capabilities": list(self.dex_adapter.capabilities),
                "bridge_adapter_capabilities": list(self.bridge_adapter.capabilities),
                "cex_adapter_capabilities": list(self.cex_adapter.capabilities),
                "cex_withdrawal_enabled": False,
                "no_cex_withdrawal_submit": True,
                "no_private_key_signing": True,
                "no_raw_signed_transaction": True,
            },
        )
        if not run.get("created", True):
            return existing_run_response(run, non_ok_statuses=NON_OK_EXISTING_RUN_STATUSES)
        approval_id = _approval_id(approval or {})
        if approval_id:
            consumed = self.store.consume_operator_approval(int(approval_id), run_id=int(run["id"]))
            if not consumed or int(consumed.get("consumed_run_id") or 0) != int(run["id"]):
                blocked = self.store.update_execution_run(
                    int(run["id"]),
                    status="BLOCKED",
                    error_code="operator_approval_already_consumed",
                    error_msg="operator_approval_already_consumed",
                )
                return {"ok": False, "existing": False, "run": blocked}
        for step_key in ROUTE_STEPS[route_type]:
            self.store.insert_execution_step(
                run_id=int(run["id"]),
                step_key=step_key,
                status="PENDING",
                payload={"mode": "live_full", "route_type": route_type, "simulated": True},
            )

        self._append_log(
            run=run,
            route_type=route_type,
            message="live_full simulated route started",
            payload={"status": "ENTERING", "run_status": "ENTERING", "route_type": route_type},
        )
        advanced = self.advance_run(int(run["id"]), trade_amount_krw=trade_amount_krw)
        final_run = advanced["run"]
        return {"ok": str(final_run.get("status")) == "SETTLED", "existing": False, "run": final_run}

    def advance_run(self, run_id: int, *, trade_amount_krw: float | None = None) -> dict[str, Any]:
        run = self.store.get_execution_run(run_id)
        if not run:
            raise ValueError("execution_run_not_found")
        if str(run.get("mode")) != "live_full":
            raise ValueError("live_full_mode_required")
        payload = run.get("payload") if isinstance(run.get("payload"), Mapping) else {}
        gate_blockers = self._gate_blockers(
            opportunity_id=int(run["opportunity_id"]),
            route_id=int(run["route_id"]),
            mode="live_full",
            trade_amount_krw=trade_amount_krw if trade_amount_krw is not None else payload.get("trade_amount_krw"),
            engine_gate_checked=bool(payload.get("engine_gate_checked")),
            run_id=run_id,
        )
        if gate_blockers:
            blocked = self.store.update_execution_run(
                run_id,
                status="BLOCKED",
                error_code=",".join(gate_blockers),
                error_msg="; ".join(gate_blockers),
            )
            self._append_log(
                run=blocked,
                route_type=str(payload.get("route_type") or ""),
                message="live_full gate recheck blocked route",
                payload={"status": "BLOCKED", "blockers": gate_blockers},
            )
            return {"run": blocked, "completed": False}

        route = self.store.get_route(int(run["route_id"])) or {}
        opportunity = self.store.get_opportunity(int(run["opportunity_id"])) or {}
        route_type = str(route.get("route_type") or _run_route_type(run))
        current_ms = int(run["started_at_ms"])

        for step in self.store.fetch_execution_steps(run_id):
            step_key = str(step["step_key"])
            status = str(step.get("status") or "")
            if status == "COMPLETED":
                current_ms = max(current_ms, int(step.get("completed_at_ms") or current_ms))
                continue
            if status in {"RECONCILE", "FAILED"}:
                return {"run": run, "completed": False}

            duration_ms = int(PAPER_STEP_DURATIONS_MS.get(step_key, 100))
            started_at_ms = current_ms
            completed_at_ms = started_at_ms + duration_ms
            self._start_step(
                run,
                route_type,
                step_key,
                started_at_ms=started_at_ms,
                duration_ms=duration_ms,
            )

            result: dict[str, Any] = {"ok": True, "payload": {}}
            if step_key in DEX_STEPS:
                result = self._execute_dex_step(
                    run=run,
                    route=route,
                    step=step,
                    step_key=step_key,
                    route_type=route_type,
                    amount_krw=trade_amount_krw,
                    started_at_ms=started_at_ms,
                    completed_at_ms=completed_at_ms,
                    duration_ms=duration_ms,
                )
            elif step_key in BRIDGE_STEPS:
                result = self._execute_bridge_step(
                    run=run,
                    route=route,
                    step=step,
                    step_key=step_key,
                    route_type=route_type,
                    amount_krw=trade_amount_krw,
                    started_at_ms=started_at_ms,
                    completed_at_ms=completed_at_ms,
                    duration_ms=duration_ms,
                )
            elif step_key in CEX_DEPOSIT_STEPS:
                result = self._execute_cex_deposit_step(
                    run=run,
                    route=route,
                    step=step,
                    step_key=step_key,
                    route_type=route_type,
                    amount_krw=trade_amount_krw,
                    started_at_ms=started_at_ms,
                    completed_at_ms=completed_at_ms,
                    duration_ms=duration_ms,
                )
            elif step_key in CEX_ORDER_STEPS:
                result = self._execute_cex_order_step(
                    run=run,
                    route=route,
                    step=step,
                    step_key=step_key,
                    route_type=route_type,
                    amount_krw=trade_amount_krw,
                    started_at_ms=started_at_ms,
                    completed_at_ms=completed_at_ms,
                    duration_ms=duration_ms,
                )

            if not result.get("ok"):
                return {"run": result["run"], "step": result["step"], "completed": False}

            self._complete_step(
                run=run,
                route_type=route_type,
                step_key=step_key,
                started_at_ms=started_at_ms,
                completed_at_ms=completed_at_ms,
                duration_ms=duration_ms,
                extra_payload=result.get("payload"),
            )
            self._apply_success_side_effects(
                run=run,
                route=route,
                opportunity=opportunity,
                route_type=route_type,
                step_key=step_key,
                observed_at_ms=completed_at_ms,
                trade_amount_krw=trade_amount_krw,
            )
            current_ms = completed_at_ms

        settled = self.store.update_execution_run(run_id, status="SETTLED")
        position = self._latest_position_for_run(run_id)
        self._append_log(
            run=settled,
            route_type=route_type,
            message="live_full simulated route settled",
            payload={
                "status": "SETTLED",
                "run_status": "SETTLED",
                "realized_pnl_krw": (position or {}).get("realized_pnl_krw"),
                "cex_withdrawal_enabled": False,
            },
        )
        return {"run": settled, "completed": True}

    def _gate_blockers(
        self,
        *,
        opportunity_id: int,
        route_id: int,
        mode: str,
        trade_amount_krw: float | None,
        engine_gate_checked: bool,
        run_id: int | None = None,
    ) -> list[str]:
        if not engine_gate_checked or self.gate_checker is None:
            return ["engine_gate_required"]
        return list(
            self.gate_checker(
                opportunity_id=opportunity_id,
                route_id=route_id,
                mode=mode,
                trade_amount_krw=trade_amount_krw,
                run_id=run_id,
            )
        )

    def _execute_dex_step(
        self,
        *,
        run: Mapping[str, Any],
        route: Mapping[str, Any],
        step: Mapping[str, Any],
        step_key: str,
        route_type: str,
        amount_krw: float | None,
        started_at_ms: int,
        completed_at_ms: int,
        duration_ms: int,
    ) -> dict[str, Any]:
        request = self._dex_request(run=run, route=route, step_key=step_key, route_type=route_type, amount_krw=amount_krw)
        try:
            quote = self.dex_adapter.quote(request)
            if not _adapter_success(quote.status):
                return self._mark_adapter_not_success(
                    run=run,
                    route_type=route_type,
                    step_key=step_key,
                    phase="quote",
                    adapter_status=quote.status,
                    evidence=quote.to_dict(),
                    external_ref="",
                    started_at_ms=started_at_ms,
                    completed_at_ms=completed_at_ms,
                    duration_ms=duration_ms,
                )
            build = self.dex_adapter.build(request, quote)
            if not _adapter_success(build.status):
                return self._mark_adapter_not_success(
                    run=run,
                    route_type=route_type,
                    step_key=step_key,
                    phase="build",
                    adapter_status=build.status,
                    evidence=build.to_dict(),
                    external_ref=build.build_ref,
                    started_at_ms=started_at_ms,
                    completed_at_ms=completed_at_ms,
                    duration_ms=duration_ms,
                )
            submit_result = self.dex_adapter.submit(request, build)
            _validate_dex_submit_result(submit_result)
            self.store.record_dry_run_transaction(
                chain_id=request.chain,
                tx_hash=submit_result.tx_hash,
                run_id=int(run["id"]),
                step_id=int(step["id"]),
                tx_type=f"{step_key}_live_full_dex_swap",
                adapter_name=submit_result.adapter_name,
                submit_ref=submit_result.submit_ref,
                status=f"DRY_RUN_{submit_result.status}",
                payload={
                    **_redact_sensitive(submit_result.to_dict()),
                    "mode": "live_full",
                    "route_type": route_type,
                    "step_key": step_key,
                    "simulated": True,
                    "live_full_boundary": "deterministic_adapter",
                    "cex_withdrawal_enabled": False,
                },
            )
            if not _adapter_success(submit_result.status):
                return self._mark_adapter_not_success(
                    run=run,
                    route_type=route_type,
                    step_key=step_key,
                    phase="submit",
                    adapter_status=submit_result.status,
                    evidence=submit_result.to_dict(),
                    external_ref=submit_result.tx_hash or submit_result.submit_ref,
                    started_at_ms=started_at_ms,
                    completed_at_ms=completed_at_ms,
                    duration_ms=duration_ms,
                )
            status = self.dex_adapter.status(request, submit_result)
            if not _adapter_success(status.status):
                return self._mark_adapter_not_success(
                    run=run,
                    route_type=route_type,
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
                route_type=route_type,
                step_key=step_key,
                error_code="live_full_adapter_error",
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
                "simulated": True,
                "quote_evidence": _redact_sensitive(submit_result.quote_evidence),
                "build_evidence": _redact_sensitive(submit_result.build_evidence),
                "payload_evidence": _redact_sensitive(submit_result.payload_evidence),
                "status_evidence": _redact_sensitive(status.evidence),
            },
        }

    def _execute_bridge_step(
        self,
        *,
        run: Mapping[str, Any],
        route: Mapping[str, Any],
        step: Mapping[str, Any],
        step_key: str,
        route_type: str,
        amount_krw: float | None,
        started_at_ms: int,
        completed_at_ms: int,
        duration_ms: int,
    ) -> dict[str, Any]:
        request = self._bridge_request(run=run, route=route, step_key=step_key, route_type=route_type, amount_krw=amount_krw)
        try:
            quote = self.bridge_adapter.quote(request)
            if not _adapter_success(quote.status):
                return self._mark_adapter_not_success(
                    run=run,
                    route_type=route_type,
                    step_key=step_key,
                    phase="quote",
                    adapter_status=quote.status,
                    evidence=quote.to_dict(),
                    external_ref=quote.bridge_ref,
                    started_at_ms=started_at_ms,
                    completed_at_ms=completed_at_ms,
                    duration_ms=duration_ms,
                )
            build = self.bridge_adapter.build(request, quote)
            if not _adapter_success(build.status):
                return self._mark_adapter_not_success(
                    run=run,
                    route_type=route_type,
                    step_key=step_key,
                    phase="build",
                    adapter_status=build.status,
                    evidence=build.to_dict(),
                    external_ref=build.build_ref or build.bridge_ref,
                    started_at_ms=started_at_ms,
                    completed_at_ms=completed_at_ms,
                    duration_ms=duration_ms,
                )
            submit_result = self.bridge_adapter.submit(request, build)
            self._upsert_bridge_transfer(
                run=run,
                step=step,
                request=request,
                status=submit_result.status,
                evidence=submit_result.to_dict(),
            )
            if not _adapter_success(submit_result.status):
                return self._mark_adapter_not_success(
                    run=run,
                    route_type=route_type,
                    step_key=step_key,
                    phase="submit",
                    adapter_status=submit_result.status,
                    evidence=submit_result.to_dict(),
                    external_ref=submit_result.bridge_ref or submit_result.submit_ref,
                    started_at_ms=started_at_ms,
                    completed_at_ms=completed_at_ms,
                    duration_ms=duration_ms,
                )
            status = self.bridge_adapter.status(request, submit_result)
            self._upsert_bridge_transfer(
                run=run,
                step=step,
                request=request,
                status=status.status,
                evidence={**status.to_dict(), "submit_evidence": submit_result.to_dict()},
            )
            if not _adapter_success(status.status):
                reconcile = self.bridge_adapter.reconcile(request, submit_result)
                self._upsert_bridge_transfer(
                    run=run,
                    step=step,
                    request=request,
                    status=reconcile.status,
                    evidence={**reconcile.to_dict(), "status_evidence": status.to_dict()},
                )
                if not _adapter_success(reconcile.status):
                    return self._mark_adapter_not_success(
                        run=run,
                        route_type=route_type,
                        step_key=step_key,
                        phase="status",
                        adapter_status=reconcile.status,
                        evidence=reconcile.to_dict(),
                        external_ref=reconcile.bridge_ref or reconcile.submit_ref,
                        started_at_ms=started_at_ms,
                        completed_at_ms=completed_at_ms,
                        duration_ms=duration_ms,
                    )
        except Exception as exc:
            return self._mark_adapter_error(
                run=run,
                route_type=route_type,
                step_key=step_key,
                error_code="live_full_adapter_error",
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
                "bridge_ref": submit_result.bridge_ref,
                "dry_run": submit_result.dry_run,
                "simulated": submit_result.simulated,
                "quote_evidence": _redact_sensitive(submit_result.quote_evidence),
                "build_evidence": _redact_sensitive(submit_result.build_evidence),
                "payload_evidence": _redact_sensitive(submit_result.payload_evidence),
                "status_evidence": _redact_sensitive(status.to_dict()),
            },
        }

    def _execute_cex_deposit_step(
        self,
        *,
        run: Mapping[str, Any],
        route: Mapping[str, Any],
        step: Mapping[str, Any],
        step_key: str,
        route_type: str,
        amount_krw: float | None,
        started_at_ms: int,
        completed_at_ms: int,
        duration_ms: int,
    ) -> dict[str, Any]:
        request = self._cex_request(run=run, route=route, step_key=step_key, route_type=route_type, amount_krw=amount_krw)
        try:
            status = self.cex_adapter.deposit_status(request)
            self._upsert_cex_deposit_transfer(run=run, step=step, request=request, status=status.status, evidence=status.to_dict())
            if not _adapter_success(status.status):
                return self._mark_adapter_not_success(
                    run=run,
                    route_type=route_type,
                    step_key=step_key,
                    phase="deposit_status",
                    adapter_status=status.status,
                    evidence=status.to_dict(),
                    external_ref=status.deposit_ref,
                    started_at_ms=started_at_ms,
                    completed_at_ms=completed_at_ms,
                    duration_ms=duration_ms,
                )
        except Exception as exc:
            return self._mark_adapter_error(
                run=run,
                route_type=route_type,
                step_key=step_key,
                error_code="live_full_adapter_error",
                error_msg=str(exc),
                started_at_ms=started_at_ms,
                completed_at_ms=completed_at_ms,
                duration_ms=duration_ms,
            )
        return {
            "ok": True,
            "payload": {
                "adapter_name": status.adapter_name,
                "deposit_ref": status.deposit_ref,
                "dry_run": status.dry_run,
                "simulated": status.simulated,
                "payload_evidence": _redact_sensitive(status.payload_evidence),
            },
        }

    def _execute_cex_order_step(
        self,
        *,
        run: Mapping[str, Any],
        route: Mapping[str, Any],
        step: Mapping[str, Any],
        step_key: str,
        route_type: str,
        amount_krw: float | None,
        started_at_ms: int,
        completed_at_ms: int,
        duration_ms: int,
    ) -> dict[str, Any]:
        request = self._cex_request(run=run, route=route, step_key=step_key, route_type=route_type, amount_krw=amount_krw)
        try:
            submit_result = self.cex_adapter.submit_order(request)
            self._upsert_cex_order(
                run=run,
                route=route,
                step=step,
                request=request,
                status=submit_result.status,
                external_order_id=submit_result.order_ref,
                evidence=submit_result.to_dict(),
            )
            if not _adapter_success(submit_result.status):
                return self._mark_adapter_not_success(
                    run=run,
                    route_type=route_type,
                    step_key=step_key,
                    phase="order_submit",
                    adapter_status=submit_result.status,
                    evidence=submit_result.to_dict(),
                    external_ref=submit_result.order_ref,
                    started_at_ms=started_at_ms,
                    completed_at_ms=completed_at_ms,
                    duration_ms=duration_ms,
                )
            reconcile = self.cex_adapter.reconcile_order(request, submit_result)
            _validate_cex_reconcile_matches(submit_result, reconcile)
            self._upsert_cex_order(
                run=run,
                route=route,
                step=step,
                request=request,
                status=reconcile.status,
                external_order_id=reconcile.order_ref,
                evidence={**reconcile.to_dict(), "submit_evidence": submit_result.to_dict()},
                avg_price_krw=_order_avg_price_krw(route, request.amount_krw),
            )
            if not _adapter_success(reconcile.status):
                return self._mark_adapter_not_success(
                    run=run,
                    route_type=route_type,
                    step_key=step_key,
                    phase="order_reconcile",
                    adapter_status=reconcile.status,
                    evidence=reconcile.to_dict(),
                    external_ref=reconcile.order_ref,
                    started_at_ms=started_at_ms,
                    completed_at_ms=completed_at_ms,
                    duration_ms=duration_ms,
                )
        except Exception as exc:
            return self._mark_adapter_error(
                run=run,
                route_type=route_type,
                step_key=step_key,
                error_code="live_full_adapter_error",
                error_msg=str(exc),
                started_at_ms=started_at_ms,
                completed_at_ms=completed_at_ms,
                duration_ms=duration_ms,
            )
        return {
            "ok": True,
            "payload": {
                "adapter_name": submit_result.adapter_name,
                "order_ref": submit_result.order_ref,
                "filled_amount_krw": reconcile.filled_amount_krw,
                "dry_run": submit_result.dry_run,
                "simulated": submit_result.simulated,
                "submit_evidence": _redact_sensitive(submit_result.to_dict()),
                "reconcile_evidence": _redact_sensitive(reconcile.to_dict()),
            },
        }

    def _dex_request(
        self,
        *,
        run: Mapping[str, Any],
        route: Mapping[str, Any],
        step_key: str,
        route_type: str,
        amount_krw: float | None,
    ) -> DexSwapRequest:
        buy_market = self.store.get_market_detail(int(route.get("buy_market_id") or 0)) or {}
        sell_market = self.store.get_market_detail(int(route.get("sell_market_id") or 0)) or {}
        route_payload = _route_payload(route)
        pool_ca = (
            buy_market.get("pool_ca")
            if step_key == "dex_buy"
            else sell_market.get("pool_ca") or route_payload.get("sell_pool_ca") or buy_market.get("pool_ca")
        )
        trade_amount = _trade_amount_krw(self.store, run=run, route_id=int(route["id"]), trade_amount_krw=amount_krw)
        slippage_bps = _slippage_bps(route_payload)
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
                "mode": "live_full",
                "route_type": route_type,
                "dry_run": bool(getattr(self.dex_adapter, "dry_run", False)),
                "simulated": True,
                "adapter_capabilities": list(self.dex_adapter.capabilities),
                "route_payload": _redact_sensitive(dict(route_payload)),
                "dry_run_simulation": route_payload.get("dry_run_simulation"),
            },
        )

    def _bridge_request(
        self,
        *,
        run: Mapping[str, Any],
        route: Mapping[str, Any],
        step_key: str,
        route_type: str,
        amount_krw: float | None,
    ) -> BridgeSubmitRequest:
        buy_market = self.store.get_market_detail(int(route.get("buy_market_id") or 0)) or {}
        sell_market = self.store.get_market_detail(int(route.get("sell_market_id") or 0)) or {}
        route_payload = _route_payload(route)
        source_chain = str(route_payload.get("source_chain") or buy_market.get("chain") or "").upper()
        destination_chain = str(
            route_payload.get("destination_chain")
            or route_payload.get("bridge_destination_chain")
            or (sell_market.get("chain") if str(sell_market.get("chain") or "").upper() != "KRW" else "")
            or sell_market.get("deposit_network")
            or source_chain
        ).upper()
        return BridgeSubmitRequest(
            opportunity_id=int(run["opportunity_id"]),
            route_id=int(run["route_id"]),
            run_id=int(run["id"]),
            step_key=step_key,
            route_type=route_type,
            source_chain=source_chain,
            destination_chain=destination_chain,
            token_ca=str(buy_market.get("token_ca") or sell_market.get("token_ca") or route_payload.get("token_ca") or ""),
            amount_krw=_trade_amount_krw(self.store, run=run, route_id=int(route["id"]), trade_amount_krw=amount_krw),
            slippage_bps=_slippage_bps(route_payload),
            idempotency_key=f"{run['id']}:{step_key}:{run['idempotency_key']}",
            source_venue=str(buy_market.get("venue") or ""),
            destination_venue=str(sell_market.get("venue") or ""),
            pool_ca=str(route_payload.get("bridge_pool_ca") or buy_market.get("pool_ca") or sell_market.get("pool_ca") or ""),
            cex_market=str(sell_market.get("market") or ""),
            deposit_network=str(sell_market.get("deposit_network") or ""),
            provider_refs=_redact_sensitive(dict(route_payload.get("provider_refs") or {})),
            payload={"route_payload": _redact_sensitive(dict(route_payload)), "bridge_simulation": route_payload.get("bridge_simulation")},
        )

    def _cex_request(
        self,
        *,
        run: Mapping[str, Any],
        route: Mapping[str, Any],
        step_key: str,
        route_type: str,
        amount_krw: float | None,
    ) -> CexTradeRequest:
        buy_market = self.store.get_market_detail(int(route.get("buy_market_id") or 0)) or {}
        sell_market = self.store.get_market_detail(int(route.get("sell_market_id") or 0)) or {}
        route_payload = _route_payload(route)
        provider_refs = dict(route_payload.get("provider_refs") or {})
        for transfer in self.store.fetch_transfers_for_run_step(int(run["id"])):
            payload = transfer.get("payload") if isinstance(transfer.get("payload"), Mapping) else {}
            for ref_key in ("bridge_ref", "submit_ref", "deposit_ref"):
                if payload.get(ref_key):
                    provider_refs.setdefault(ref_key, payload.get(ref_key))
        return CexTradeRequest(
            opportunity_id=int(run["opportunity_id"]),
            route_id=int(run["route_id"]),
            run_id=int(run["id"]),
            step_key=step_key,
            route_type=route_type,
            source_venue=str(buy_market.get("venue") or ""),
            destination_venue=str(sell_market.get("venue") or ""),
            cex_market=str(sell_market.get("market") or ""),
            deposit_network=str(sell_market.get("deposit_network") or ""),
            token_ca=str(buy_market.get("token_ca") or sell_market.get("token_ca") or route_payload.get("token_ca") or ""),
            amount_krw=_trade_amount_krw(self.store, run=run, route_id=int(route["id"]), trade_amount_krw=amount_krw),
            slippage_bps=_slippage_bps(route_payload),
            idempotency_key=f"{run['id']}:{step_key}:{run['idempotency_key']}",
            source_chain=str(buy_market.get("chain") or ""),
            destination_chain=str(sell_market.get("chain") or ""),
            pool_ca=str(buy_market.get("pool_ca") or sell_market.get("pool_ca") or ""),
            provider_refs=_redact_sensitive(provider_refs),
            payload={"route_payload": _redact_sensitive(dict(route_payload)), "cex_simulation": route_payload.get("cex_simulation")},
        )

    def _upsert_bridge_transfer(
        self,
        *,
        run: Mapping[str, Any],
        step: Mapping[str, Any],
        request: BridgeSubmitRequest,
        status: str,
        evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        transfer = self.store.upsert_transfer(
            transfer_key=f"live_full_bridge:{run['id']}:{request.step_key}",
            run_id=int(run["id"]),
            step_id=int(step["id"]),
            from_location=request.source_chain,
            to_location=request.destination_chain,
            status=f"SIMULATED_{str(status or 'unknown').upper()}",
            amount_raw="",
            payload={
                **_redact_sensitive(dict(evidence)),
                "mode": "live_full",
                "route_type": request.route_type,
                "step_key": request.step_key,
                "cex_withdrawal_enabled": False,
            },
        )
        self._append_transfer_update(run=run, transfer=transfer, step_key=request.step_key, status=str(status or "unknown"))
        return transfer

    def _upsert_cex_deposit_transfer(
        self,
        *,
        run: Mapping[str, Any],
        step: Mapping[str, Any],
        request: CexTradeRequest,
        status: str,
        evidence: Mapping[str, Any],
    ) -> dict[str, Any]:
        transfer = self.store.upsert_transfer(
            transfer_key=f"live_full_cex_deposit:{run['id']}:{request.step_key}",
            run_id=int(run["id"]),
            step_id=int(step["id"]),
            from_location=request.source_chain or request.deposit_network,
            to_location=request.destination_venue,
            status=f"SIMULATED_{str(status or 'unknown').upper()}",
            amount_raw="",
            payload={
                **_redact_sensitive(dict(evidence)),
                "mode": "live_full",
                "route_type": request.route_type,
                "step_key": request.step_key,
                "cex_withdrawal_enabled": False,
                "cex_withdrawal_submit": False,
            },
        )
        self._append_transfer_update(run=run, transfer=transfer, step_key=request.step_key, status=str(status or "unknown"))
        return transfer

    def _upsert_cex_order(
        self,
        *,
        run: Mapping[str, Any],
        route: Mapping[str, Any],
        step: Mapping[str, Any],
        request: CexTradeRequest,
        status: str,
        external_order_id: str,
        evidence: Mapping[str, Any],
        avg_price_krw: float | None = None,
    ) -> dict[str, Any]:
        sell_market = self.store.get_market_detail(int(route.get("sell_market_id") or 0)) or {}
        order = self.store.upsert_order(
            order_key=f"live_full_cex_order:{run['id']}:{request.step_key}",
            run_id=int(run["id"]),
            step_id=int(step["id"]),
            venue_code=str(sell_market.get("venue") or request.destination_venue),
            market_key=str(sell_market.get("market_key") or request.cex_market),
            side="SELL",
            order_type="MARKET",
            amount_raw="",
            amount_value_krw=float(request.amount_krw),
            avg_price_krw=avg_price_krw,
            status=f"SIMULATED_{str(status or 'unknown').upper()}",
            external_order_id=external_order_id,
            payload={
                **_redact_sensitive(dict(evidence)),
                "mode": "live_full",
                "route_type": request.route_type,
                "step_key": request.step_key,
                "cex_withdrawal_enabled": False,
                "cex_withdrawal_submit": False,
                "real_cex_order": False,
            },
        )
        self._append_order_update(run=run, order=order, step_key=request.step_key, status=str(status or "unknown"))
        return order

    def _start_step(
        self,
        run: Mapping[str, Any],
        route_type: str,
        step_key: str,
        *,
        started_at_ms: int,
        duration_ms: int,
    ) -> None:
        self.store.update_execution_step(
            run_id=int(run["id"]),
            step_key=step_key,
            status="RUNNING",
            started_at_ms=started_at_ms,
            duration_ms=duration_ms,
            payload={"mode": "live_full", "route_type": route_type, "transition": "started", "simulated": True},
        )
        self.store.append_event(
            event_type="execution.step.started",
            opportunity_id=int(run["opportunity_id"]),
            route_id=int(run["route_id"]),
            run_id=int(run["id"]),
            payload={
                "step_key": step_key,
                "status": "RUNNING",
                "started_at_ms": started_at_ms,
                "mode": "live_full",
                "route_type": route_type,
                "simulated": True,
            },
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
            route_type=route_type,
            message=f"live_full step started: {step_key}",
            payload={"step_key": step_key, "status": "RUNNING"},
        )

    def _complete_step(
        self,
        *,
        run: Mapping[str, Any],
        route_type: str,
        step_key: str,
        started_at_ms: int,
        completed_at_ms: int,
        duration_ms: int,
        extra_payload: Mapping[str, Any] | None = None,
    ) -> None:
        payload = {
            "mode": "live_full",
            "route_type": route_type,
            "transition": "completed",
            "simulated": True,
            "cex_withdrawal_enabled": False,
            **_redact_sensitive(dict(extra_payload or {})),
        }
        self.store.update_execution_step(
            run_id=int(run["id"]),
            step_key=step_key,
            status="COMPLETED",
            external_ref=str(payload.get("tx_hash") or payload.get("bridge_ref") or payload.get("deposit_ref") or payload.get("order_ref") or payload.get("submit_ref") or ""),
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
                "mode": "live_full",
                "route_type": route_type,
                "simulated": True,
                "tx_hash": payload.get("tx_hash"),
                "submit_ref": payload.get("submit_ref"),
                "bridge_ref": payload.get("bridge_ref"),
                "deposit_ref": payload.get("deposit_ref"),
                "order_ref": payload.get("order_ref"),
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
            extra_payload={
                "tx_hash": payload.get("tx_hash"),
                "submit_ref": payload.get("submit_ref"),
                "bridge_ref": payload.get("bridge_ref"),
                "deposit_ref": payload.get("deposit_ref"),
                "order_ref": payload.get("order_ref"),
            },
        )
        self._append_log(
            run=run,
            route_type=route_type,
            message=f"live_full step completed: {step_key}",
            payload={"step_key": step_key, "status": "COMPLETED", "duration_ms": duration_ms},
        )

    def _mark_adapter_not_success(
        self,
        *,
        run: Mapping[str, Any],
        route_type: str,
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
        error_code = f"live_full_adapter_{phase}_{status_key or 'not_success'}"
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
                "mode": "live_full",
                "route_type": route_type,
                "transition": "reconcile" if reconcile else "failed",
                "phase": phase,
                "adapter_status": adapter_status,
                "evidence": _redact_sensitive(dict(evidence)),
                "cex_withdrawal_enabled": False,
            },
        )
        updated = self.store.update_execution_run(
            int(run["id"]),
            status=run_status,
            error_code=error_code,
            error_msg=f"live_full route stopped at {step_key}",
        )
        reason = "unknown_external_outcome" if status_key == "unknown" else (
            "live_full_reconcile_required" if reconcile else "live_full_adapter_not_success"
        )
        self.store.append_dead_letter(
            reason=reason,
            deadletter_key=f"live_full_adapter:{run['id']}:{step_key}:{phase}",
            error_code=error_code,
            retryable=False,
            payload={
                "run_id": int(run["id"]),
                "route_id": int(run["route_id"]),
                "step_key": step_key,
                "phase": phase,
                "adapter_status": adapter_status,
                "provider_ref": refs["provider_ref"],
                "external_ref": refs["external_ref"],
                "tx_hash": refs["tx_hash"],
                "submit_ref": refs["submit_ref"],
                "bridge_ref": refs["bridge_ref"],
                "deposit_ref": refs["deposit_ref"],
                "order_ref": refs["order_ref"],
                "error_code": error_code,
                "safe_retry": False,
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
                "mode": "live_full",
                "route_type": route_type,
                "simulated": True,
                "phase": phase,
                "error_code": error_code,
                "adapter_status": adapter_status,
                "provider_ref": refs["provider_ref"],
                "external_ref": refs["external_ref"],
                "tx_hash": refs["tx_hash"],
                "submit_ref": refs["submit_ref"],
                "bridge_ref": refs["bridge_ref"],
                "deposit_ref": refs["deposit_ref"],
                "order_ref": refs["order_ref"],
            },
        )
        self._append_flow_updates(
            run=updated,
            route_type=route_type,
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
                "bridge_ref": refs["bridge_ref"],
                "deposit_ref": refs["deposit_ref"],
                "order_ref": refs["order_ref"],
            },
        )
        self._append_log(
            run=updated,
            route_type=route_type,
            message=f"live_full route stopped: {step_key}",
            severity="warning" if reconcile else "error",
            payload={"step_key": step_key, "status": step_status, "run_status": run_status, "error_code": error_code},
        )
        return {"ok": False, "run": updated, "step": step}

    def _mark_adapter_error(
        self,
        *,
        run: Mapping[str, Any],
        route_type: str,
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
            payload={
                "mode": "live_full",
                "route_type": route_type,
                "transition": "failed",
                "error_msg": error_msg,
                "cex_withdrawal_enabled": False,
            },
        )
        updated = self.store.update_execution_run(int(run["id"]), status="FAILED", error_code=error_code, error_msg=error_msg)
        self.store.append_dead_letter(
            reason="live_full_adapter_error",
            deadletter_key=f"live_full_adapter_error:{run['id']}:{step_key}",
            error_code=error_code,
            retryable=False,
            payload={
                "run_id": int(run["id"]),
                "route_id": int(run["route_id"]),
                "step_key": step_key,
                "error_msg": error_msg,
                "safe_retry": False,
            },
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
                "mode": "live_full",
                "route_type": route_type,
                "error_code": error_code,
            },
        )
        self._append_flow_updates(
            run=updated,
            route_type=route_type,
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
            route_type=route_type,
            message=f"live_full route failed: {step_key}",
            severity="error",
            payload={"step_key": step_key, "status": "FAILED", "run_status": "FAILED", "error_code": error_code},
        )
        return {"ok": False, "run": updated, "step": step}

    def _apply_success_side_effects(
        self,
        *,
        run: Mapping[str, Any],
        route: Mapping[str, Any],
        opportunity: Mapping[str, Any],
        route_type: str,
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
                route_type=route_type,
                status="OPEN",
                observed_at_ms=observed_at_ms,
                trade_amount_krw=trade_amount_krw,
                closed=False,
            )
            self._append_position_update(run, position, route_type=route_type, status="OPEN")
        elif step_key == "exit_route_select":
            self.store.update_execution_run(int(run["id"]), status="EXITING")
        elif step_key in EXIT_STEPS or step_key in {"bridge", "cex_deposit"}:
            position = self._upsert_position(
                run=run,
                opportunity=opportunity,
                route=route,
                route_type=route_type,
                status="EXITING",
                observed_at_ms=observed_at_ms,
                trade_amount_krw=trade_amount_krw,
                closed=False,
            )
            self._append_position_update(run, position, route_type=route_type, status="EXITING")
        elif step_key == "settle":
            position = self._upsert_position(
                run=run,
                opportunity=opportunity,
                route=route,
                route_type=route_type,
                status="SETTLED",
                observed_at_ms=observed_at_ms,
                trade_amount_krw=trade_amount_krw,
                closed=True,
            )
            self._append_position_update(run, position, route_type=route_type, status="SETTLED")

    def _upsert_position(
        self,
        *,
        run: Mapping[str, Any],
        opportunity: Mapping[str, Any],
        route: Mapping[str, Any],
        route_type: str,
        status: str,
        observed_at_ms: int,
        trade_amount_krw: float | None,
        closed: bool,
    ) -> dict[str, Any]:
        values = self._position_values(route=route, run=run, trade_amount_krw=trade_amount_krw)
        position = self.store.upsert_position(
            position_key=f"live_full:{run['id']}",
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
                "mode": "live_full",
                "route_type": route_type,
                "current_status": status,
                "live_exit_estimate_krw": values["live_exit_estimate_krw"],
                "pnl_placeholder_krw": values["pnl_placeholder_krw"],
                "simulated": True,
                "dry_run": bool(run.get("payload", {}).get("dry_run")) if isinstance(run.get("payload"), Mapping) else True,
                "adapter_boundary": "deterministic_default_or_explicit_adapter",
                "cex_withdrawal_enabled": False,
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
                "mode": "live_full",
                "route_type": route_type,
                "simulated": True,
                "cex_withdrawal_enabled": False,
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

    def _latest_position_for_run(self, run_id: int) -> dict[str, Any] | None:
        positions = self.store.fetch_positions(run_id=run_id)
        return positions[0] if positions else None

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
            "mode": "live_full",
            "simulated": True,
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

    def _append_log(
        self,
        *,
        run: Mapping[str, Any],
        route_type: str,
        message: str,
        payload: Mapping[str, Any] | None = None,
        severity: str = "info",
    ) -> None:
        merged = {
            "message": message,
            "mode": "live_full",
            "route_type": route_type,
            "simulated": True,
            "cex_withdrawal_enabled": False,
            **dict(payload or {}),
        }
        self.store.append_event(
            event_type="execution.log.append",
            opportunity_id=int(run["opportunity_id"]),
            route_id=int(run["route_id"]),
            run_id=int(run["id"]),
            severity=severity,
            payload=merged,
        )

    def _append_position_update(self, run: Mapping[str, Any], position: Mapping[str, Any], *, route_type: str, status: str) -> None:
        self.store.append_event(
            event_type="position.update",
            opportunity_id=int(run["opportunity_id"]),
            route_id=int(run["route_id"]),
            run_id=int(run["id"]),
            payload={
                "position_id": int(position["id"]),
                "status": status,
                "mode": "live_full",
                "route_type": route_type,
                "simulated": True,
                "cex_withdrawal_enabled": False,
            },
        )

    def _append_transfer_update(self, *, run: Mapping[str, Any], transfer: Mapping[str, Any], step_key: str, status: str) -> None:
        transfer_payload = transfer.get("payload") if isinstance(transfer.get("payload"), Mapping) else {}
        self.store.append_event(
            event_type="transfer.update",
            opportunity_id=int(run["opportunity_id"]),
            route_id=int(run["route_id"]),
            run_id=int(run["id"]),
            payload={
                "transfer_id": int(transfer["id"]),
                "transfer_key": transfer.get("transfer_key"),
                "step_key": step_key,
                "status": status,
                "transfer_status": transfer.get("status"),
                "from_location": transfer.get("from_location"),
                "to_location": transfer.get("to_location"),
                "bridge_ref": transfer_payload.get("bridge_ref"),
                "deposit_ref": transfer_payload.get("deposit_ref"),
                "submit_ref": transfer_payload.get("submit_ref"),
                "external_ref": (
                    transfer_payload.get("external_ref")
                    or transfer_payload.get("bridge_ref")
                    or transfer_payload.get("deposit_ref")
                    or transfer_payload.get("submit_ref")
                    or transfer.get("transfer_key")
                ),
                "adapter_name": transfer_payload.get("adapter_name"),
                "mode": "live_full",
                "simulated": True,
                "cex_withdrawal_enabled": False,
            },
        )

    def _append_order_update(self, *, run: Mapping[str, Any], order: Mapping[str, Any], step_key: str, status: str) -> None:
        order_payload = order.get("payload") if isinstance(order.get("payload"), Mapping) else {}
        self.store.append_event(
            event_type="order.update",
            opportunity_id=int(run["opportunity_id"]),
            route_id=int(run["route_id"]),
            run_id=int(run["id"]),
            payload={
                "order_id": int(order["id"]),
                "order_key": order.get("order_key"),
                "step_key": step_key,
                "status": status,
                "order_status": order.get("status"),
                "venue_code": order.get("venue_code"),
                "market_key": order.get("market_key"),
                "side": order.get("side"),
                "external_order_id": order.get("external_order_id"),
                "order_ref": order_payload.get("order_ref") or order.get("external_order_id"),
                "external_ref": order_payload.get("external_ref") or order_payload.get("order_ref") or order.get("external_order_id"),
                "adapter_name": order_payload.get("adapter_name"),
                "mode": "live_full",
                "simulated": True,
                "cex_withdrawal_enabled": False,
            },
        )


def _adapter_success(status: Any) -> bool:
    return str(status or "").strip().lower() == "success"


def _validate_dex_submit_result(result: Any) -> None:
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


def _validate_cex_reconcile_matches(submit_result: Any, reconcile: Any) -> None:
    submit_ref = str(getattr(submit_result, "order_ref", "") or "")
    reconcile_ref = str(getattr(reconcile, "order_ref", "") or "")
    if not submit_ref or not reconcile_ref or submit_ref != reconcile_ref:
        raise ValueError("cex_order_reconcile_ref_mismatch")


def _adapter_refs(*, evidence: Mapping[str, Any], external_ref: str) -> dict[str, str]:
    payload_evidence = evidence.get("payload_evidence")
    payload_map = payload_evidence if isinstance(payload_evidence, Mapping) else {}
    provider_refs = payload_map.get("provider_refs")
    provider_map = provider_refs if isinstance(provider_refs, Mapping) else {}
    tx_hash = str(evidence.get("tx_hash") or payload_map.get("tx_hash") or "")
    submit_ref = str(evidence.get("submit_ref") or payload_map.get("submit_ref") or "")
    bridge_ref = str(evidence.get("bridge_ref") or payload_map.get("bridge_ref") or "")
    deposit_ref = str(evidence.get("deposit_ref") or payload_map.get("deposit_ref") or "")
    order_ref = str(evidence.get("order_ref") or payload_map.get("order_ref") or "")
    provider_ref = str(provider_map.get("provider_ref") or provider_map.get("quote_ref") or provider_map.get("deposit_ref") or "")
    resolved_external_ref = str(external_ref or tx_hash or bridge_ref or deposit_ref or order_ref or submit_ref)
    return {
        "external_ref": resolved_external_ref,
        "provider_ref": provider_ref,
        "tx_hash": tx_hash,
        "submit_ref": submit_ref,
        "bridge_ref": bridge_ref,
        "deposit_ref": deposit_ref,
        "order_ref": order_ref,
    }


def _trade_amount_krw(
    store: ArbitrageStore,
    *,
    run: Mapping[str, Any],
    route_id: int,
    trade_amount_krw: float | None,
) -> float:
    run_payload = run.get("payload") if isinstance(run.get("payload"), Mapping) else {}
    quote = store.get_latest_route_quote(route_id)
    return _first_positive_float(
        trade_amount_krw,
        run_payload.get("trade_amount_krw") if isinstance(run_payload, Mapping) else None,
        (quote or {}).get("amount_in_value_krw"),
        100_000.0,
    )


def _route_payload(route: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = route.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _slippage_bps(route_payload: Mapping[str, Any]) -> int:
    return int(_first_positive_float(route_payload.get("slippage_bps"), route_payload.get("max_slippage_bps"), 150))


def _order_avg_price_krw(route: Mapping[str, Any], amount_krw: float) -> float | None:
    try:
        edge = float(route.get("edge_worst_bps") or 0.0)
        amount = float(amount_krw)
    except (TypeError, ValueError):
        return None
    return round(amount * (1.0 + edge / 10_000.0), 8)


def _run_route_type(run: Mapping[str, Any]) -> str:
    payload = run.get("payload") if isinstance(run.get("payload"), Mapping) else {}
    return str(payload.get("route_type") or "")


def _approval_id(approval: Mapping[str, Any]) -> int | None:
    for key in ("approval_id", "id"):
        try:
            value = int(approval.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return None


def _first_positive_float(*values: Any) -> float:
    for value in values:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return 0.0


def _redact_sensitive(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _sensitive_key(str(key)) else _redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_sensitive(item) for item in value]
    return value


def _sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(
        part in normalized
        for part in (
            "api_key",
            "apikey",
            "secret",
            "token_secret",
            "private_key",
            "password",
            "authorization",
            "signature",
            "signed_payload",
            "raw_transaction",
        )
    )
