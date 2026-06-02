from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from .collectors.base import redact_provider_payload
from .detector import ArbitrageDetector, DetectorTTLConfig
from .engine import ArbitrageEngine
from .live_collectors import LiveProviderJobRunner
from .paper_execution import ROUTE_STEPS
from .provider_scheduler import ReadOnlyPollingScheduler
from .route_evaluator import evaluate_stored_route
from .store import ArbitrageStore, now_ms


READ_ONLY_SIMULATION_FLAGS = {
    "simulation_only": True,
    "no_real_funds": True,
    "no_real_submit": True,
}
TERMINAL_OK_STATUSES = {"COMPLETED", "NO_OPPORTUNITY", "BLOCKED"}
PROVIDER_OK_STATUSES = {"OK", "ACTIVE"}
SIMULATION_REQUEST_JOB_FIELDS = {
    "provider_key",
    "capability",
    "scope_key",
    "enabled",
    "display_name",
    "job_id",
    "id",
    "provider_job_id",
    "symbol",
    "market",
    "chain",
    "chain_id",
    "chainId",
    "pair_id",
    "pairId",
    "pair_address",
    "pairAddress",
    "pool_address",
    "poolAddress",
    "network",
    "route_id",
    "limit",
    "interval_ms",
    "jitter_ms",
    "timeout_ms",
    "max_attempts",
    "max_retries",
    "fallback_enabled",
    "payload",
}


class SimulationRunner:
    """No-funds live-monitor simulation pipeline.

    The runner accepts read-only provider jobs, stores observations, detects a
    candidate from stored data, evaluates route evidence, runs precheck, and then
    executes a deterministic paper saga through the engine gate. It never calls
    real submit adapters.
    """

    def __init__(
        self,
        store: ArbitrageStore,
        *,
        provider_runner: LiveProviderJobRunner | None = None,
    ) -> None:
        self.store = store
        self.provider_runner = provider_runner or LiveProviderJobRunner(store)

    def start(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        request = dict(payload or {})
        stamp = int(request.get("now_ms") or now_ms())
        simulation = self.store.insert_simulation_run(
            simulation_key=str(request.get("simulation_key") or f"simulation:{uuid.uuid4().hex}"),
            status="RUNNING",
            requested_by=str(request.get("requested_by") or "api"),
            payload={"request": _safe_request_payload(request), **READ_ONLY_SIMULATION_FLAGS},
        )
        simulation_id = int(simulation["id"])
        if not simulation.get("created", True):
            return {
                "ok": False,
                "status": str(simulation.get("status") or "CONFLICT"),
                "simulation_run": simulation,
                "simulation_run_id": simulation_id,
                "error_code": "simulation_key_conflict",
                "error_msg": "simulation_key_conflict",
                "blockers": ["simulation_key_conflict"],
                **READ_ONLY_SIMULATION_FLAGS,
            }
        tracker = _StageTracker(self.store, simulation_id=simulation_id)
        self.store.append_event(
            event_type="simulation.run.started",
            payload={
                "simulation_run_id": simulation_id,
                "status": "RUNNING",
                **READ_ONLY_SIMULATION_FLAGS,
            },
        )

        provider_results: list[dict[str, Any]] = []
        detector_result: dict[str, Any] = {}
        edge_evaluation: dict[str, Any] | None = None
        precheck: dict[str, Any] | None = None
        paper_result: dict[str, Any] | None = None
        selected_opportunity: dict[str, Any] | None = None
        selected_route: dict[str, Any] | None = None
        blockers: list[str] = []
        before_opportunity_ids = {int(row["id"]) for row in self.store.fetch_opportunities()}

        try:
            jobs = _provider_jobs_from_request(request, self.provider_runner.default_provider_jobs())
            tracker.start("collect")
            provider_results = self._run_collect_stage(jobs, request=request, stamp=stamp)
            provider_blockers = _provider_result_blockers(provider_results)
            if provider_blockers and not _truthy(request.get("continue_on_provider_failure")):
                trackers = tracker.complete("collect", blockers=provider_blockers, summary={"provider_results": provider_results})
                return self._finish(
                    simulation_id,
                    request=request,
                    status="FAILED",
                    ok=False,
                    error_code="provider_collect_failed",
                    error_msg="; ".join(provider_blockers),
                    provider_results=provider_results,
                    detector_result=detector_result,
                    edge_evaluation=edge_evaluation,
                    precheck=precheck,
                    paper_result=paper_result,
                    selected_opportunity=selected_opportunity,
                    selected_route=selected_route,
                    blockers=provider_blockers,
                    stages=trackers,
                )
            tracker.complete("collect", summary={"provider_results": provider_results})

            tracker.start("detect")
            detector = ArbitrageDetector(
                self.store,
                spread_threshold_bps=float(request.get("spread_threshold_bps") or 0.0),
                drawdown_threshold_bps=float(request.get("drawdown_threshold_bps") or 500.0),
                lookback_ms=int(request.get("lookback_ms") or 60_000),
                ttl_config=DetectorTTLConfig.from_mapping(_mapping_or_none(request.get("ttl_config"))),
            )
            detector_result = detector.run(now_ms=stamp).to_dict()
            selected_opportunity = self._select_opportunity(request, before_opportunity_ids=before_opportunity_ids)
            if not selected_opportunity:
                blockers = _no_opportunity_blockers(request, stamp=stamp)
                status = "BLOCKED" if blockers else "NO_OPPORTUNITY"
                tracker.complete("detect", blockers=blockers, summary={"detector_result": detector_result})
                return self._finish(
                    simulation_id,
                    request=request,
                    status=status,
                    ok=True,
                    error_code="stale_observations_blocked" if blockers else "",
                    error_msg="; ".join(blockers),
                    provider_results=provider_results,
                    detector_result=detector_result,
                    edge_evaluation=edge_evaluation,
                    precheck=precheck,
                    paper_result=paper_result,
                    selected_opportunity=selected_opportunity,
                    selected_route=selected_route,
                    blockers=blockers,
                    stages=tracker.records,
                )
            selected_route = self._select_route(request, selected_opportunity)
            if not selected_route:
                blockers = ["simulation_no_route_detected"]
                tracker.complete("detect", blockers=blockers, summary={"detector_result": detector_result})
                return self._finish(
                    simulation_id,
                    request=request,
                    status="BLOCKED",
                    ok=True,
                    error_code="simulation_no_route_detected",
                    error_msg="simulation_no_route_detected",
                    provider_results=provider_results,
                    detector_result=detector_result,
                    edge_evaluation=edge_evaluation,
                    precheck=precheck,
                    paper_result=paper_result,
                    selected_opportunity=selected_opportunity,
                    selected_route=selected_route,
                    blockers=blockers,
                    stages=tracker.records,
                )
            tracker.complete(
                "detect",
                summary={
                    "detector_result": detector_result,
                    "opportunity_id": int(selected_opportunity["id"]),
                    "route_id": int(selected_route["id"]),
                },
            )

            tracker.start("evaluate")
            self._prepare_simulation_edge_evidence(selected_route, request=request, stamp=stamp)
            edge_evaluation = evaluate_stored_route(self.store, int(selected_route["id"]), as_of_ms=stamp).to_dict()
            selected_route = self.store.get_route(int(selected_route["id"])) or selected_route
            blockers = _edge_blockers(edge_evaluation)
            if not blockers:
                _extend_quote_freshness_for_wall_clock(
                    self.store,
                    int(selected_route["id"]),
                    ttl_ms=int(request.get("route_quote_ttl_ms") or 30_000),
                )
                selected_route = self.store.get_route(int(selected_route["id"])) or selected_route
            tracker.complete("evaluate", blockers=blockers, summary={"edge_evaluation": edge_evaluation})

            tracker.start("precheck")
            precheck = ArbitrageEngine(self.store).run_precheck(
                opportunity_id=int(selected_opportunity["id"]),
                route_id=int(selected_route["id"]),
                checks=_precheck_checks(edge_evaluation, blockers=blockers),
            )
            selected_opportunity = self.store.get_opportunity(int(selected_opportunity["id"])) or selected_opportunity
            selected_route = self.store.get_route(int(selected_route["id"])) or selected_route
            precheck_blockers = _precheck_blockers(precheck, selected_route)
            blockers = _unique([*blockers, *precheck_blockers])
            tracker.complete("precheck", blockers=blockers, summary={"precheck": precheck})
            if blockers:
                return self._finish(
                    simulation_id,
                    request=request,
                    status="BLOCKED",
                    ok=True,
                    error_code="simulation_precheck_blocked",
                    error_msg="; ".join(blockers),
                    provider_results=provider_results,
                    detector_result=detector_result,
                    edge_evaluation=edge_evaluation,
                    precheck=precheck,
                    paper_result=paper_result,
                    selected_opportunity=selected_opportunity,
                    selected_route=selected_route,
                    blockers=blockers,
                    stages=tracker.records,
                )

            tracker.start("paper_execution")
            trade_amount_krw = _optional_float(request.get("trade_amount_krw"))
            paper_result = ArbitrageEngine(self.store).start_execution(
                opportunity_id=int(selected_opportunity["id"]),
                route_id=int(selected_route["id"]),
                mode="paper",
                idempotency_key=f"simulation-paper:{simulation_id}:{selected_opportunity['id']}:{selected_route['id']}",
                requested_by=str(request.get("requested_by") or "simulation"),
                trade_amount_krw=trade_amount_krw,
            )
            paper_blockers = _paper_blockers(paper_result)
            tracker.complete("paper_execution", blockers=paper_blockers, summary={"paper_result": _paper_summary(paper_result)})
            if paper_blockers:
                blockers = _unique([*blockers, *paper_blockers])
                return self._finish(
                    simulation_id,
                    request=request,
                    status="BLOCKED",
                    ok=True,
                    error_code="simulation_paper_execution_blocked",
                    error_msg="; ".join(blockers),
                    provider_results=provider_results,
                    detector_result=detector_result,
                    edge_evaluation=edge_evaluation,
                    precheck=precheck,
                    paper_result=paper_result,
                    selected_opportunity=selected_opportunity,
                    selected_route=selected_route,
                    blockers=blockers,
                    stages=tracker.records,
                )

            run = _paper_run(paper_result)
            return self._finish(
                simulation_id,
                request=request,
                status="COMPLETED",
                ok=True,
                provider_results=provider_results,
                detector_result=detector_result,
                edge_evaluation=edge_evaluation,
                precheck=precheck,
                paper_result=paper_result,
                selected_opportunity=selected_opportunity,
                selected_route=selected_route,
                execution_run_id=_optional_int(run.get("id")),
                blockers=blockers,
                stages=tracker.records,
            )
        except Exception as exc:
            tracker.fail_current(error_code=str(exc) or "simulation_failed")
            failed = self.store.update_simulation_run(
                simulation_id,
                status="FAILED",
                error_code=str(exc) or "simulation_failed",
                error_msg=str(exc),
                payload={
                    "request": _safe_request_payload(request),
                    "stages": tracker.records,
                    "stage_status": _stage_status(tracker.records),
                    "blockers": [str(exc)],
                    **READ_ONLY_SIMULATION_FLAGS,
                },
            )
            self.store.append_event(
                event_type="simulation.run.failed",
                severity="warning",
                payload={
                    "simulation_run_id": simulation_id,
                    "status": "FAILED",
                    "error_code": str(exc),
                    **READ_ONLY_SIMULATION_FLAGS,
                },
            )
            return {
                "ok": False,
                "status": "FAILED",
                "simulation_run": failed,
                "simulation_run_id": simulation_id,
                "error_code": str(exc) or "simulation_failed",
                "blockers": [str(exc)],
                "stages": tracker.records,
                "stage_status": _stage_status(tracker.records),
                **READ_ONLY_SIMULATION_FLAGS,
            }

    def _run_collect_stage(
        self,
        jobs: Sequence[Mapping[str, Any]],
        *,
        request: Mapping[str, Any],
        stamp: int,
    ) -> list[dict[str, Any]]:
        if not jobs:
            return []
        if _bounded_live_collect_requested(request) and not any("payload" in job for job in jobs):
            scheduler = ReadOnlyPollingScheduler(
                self.store,
                runner=self.provider_runner,
                enabled=True,
                loop_sleep_ms=0,
            )
            summary = scheduler.run_once(jobs, now_ms=stamp, force=True)
            return [result.to_dict() for result in summary.results]
        return [result.to_dict() for result in self.provider_runner.run_once(jobs, now_ms=stamp)]

    def _select_opportunity(
        self,
        request: Mapping[str, Any],
        *,
        before_opportunity_ids: set[int],
    ) -> dict[str, Any] | None:
        requested_id = _optional_int(request.get("opportunity_id") or request.get("selected_opportunity_id"))
        if requested_id is not None:
            return self.store.get_opportunity(requested_id)
        opportunities = self.store.fetch_opportunities()
        new_opportunities = [item for item in opportunities if int(item.get("id") or 0) not in before_opportunity_ids]
        return (new_opportunities or opportunities or [None])[0]

    def _select_route(self, request: Mapping[str, Any], opportunity: Mapping[str, Any]) -> dict[str, Any] | None:
        requested_route_id = _optional_int(request.get("route_id") or request.get("selected_route_id"))
        if requested_route_id is not None:
            route = self.store.get_route(requested_route_id)
            if route and int(route.get("opportunity_id") or 0) == int(opportunity.get("id") or 0):
                return route
            return None
        routes = self.store.fetch_routes_for_opportunity(int(opportunity["id"]))
        return next((item for item in routes if int(item.get("selected") or 0) == 1), routes[0] if routes else None)

    def _prepare_simulation_edge_evidence(
        self,
        route: Mapping[str, Any],
        *,
        request: Mapping[str, Any],
        stamp: int,
    ) -> None:
        route_id = int(route["id"])
        route_type = str(route.get("route_type") or "same_dex_sell")
        ttl_ms = int(request.get("route_quote_ttl_ms") or 30_000)
        fresh_until_ms = stamp + ttl_ms
        existing_payload = _mapping_or_none(route.get("payload")) or {}
        simulated_payload = _simulation_route_payload(
            existing_payload,
            route_type=route_type,
            request=request,
            stamp=stamp,
            fresh_until_ms=fresh_until_ms,
        )
        _merge_route_payload(self.store, route_id, simulated_payload)
        _ensure_simulation_buy_tick(
            self.store,
            route,
            stamp=stamp,
        )
        _ensure_simulation_exit_quote(
            self.store,
            route,
            route_type=route_type,
            request=request,
            stamp=stamp,
            fresh_until_ms=fresh_until_ms,
        )

        requested_freshness = {
            str(key): int(value)
            for key, value in (_mapping_or_none(request.get("route_freshness")) or {}).items()
            if _optional_int(value) is not None
        }
        if requested_freshness:
            self.store.set_route_freshness(route_id, requested_freshness)
        existing_freshness = self.store.fetch_route_freshness(route_id)
        additions: dict[str, int] = {}
        for key in ("rpc_block", "rpc_freshness"):
            if key not in existing_freshness:
                additions[key] = fresh_until_ms
        if route_type in {"direct_cex_sell", "bridge_cex_sell"}:
            if _status_freshness_allowed(
                existing_payload,
                request,
                request_key="deposit_status",
                payload_keys=("deposit_status", "cex_deposit_status", "cex_deposit"),
            ):
                for key in ("deposit_status", "cex_deposit"):
                    if key not in existing_freshness:
                        additions[key] = fresh_until_ms
        if route_type in {"bridge_dex_sell", "bridge_cex_sell"}:
            if _status_freshness_allowed(
                existing_payload,
                request,
                request_key="bridge_status",
                payload_keys=("bridge_status", "bridge_availability", "bridge"),
            ):
                for key in ("bridge_status", "bridge_availability"):
                    if key not in existing_freshness:
                        additions[key] = fresh_until_ms
        if additions:
            self.store.set_route_freshness(route_id, additions)

    def _finish(
        self,
        simulation_id: int,
        *,
        request: Mapping[str, Any],
        status: str,
        ok: bool,
        provider_results: list[dict[str, Any]],
        detector_result: dict[str, Any],
        edge_evaluation: dict[str, Any] | None,
        precheck: dict[str, Any] | None,
        paper_result: dict[str, Any] | None,
        selected_opportunity: dict[str, Any] | None,
        selected_route: dict[str, Any] | None,
        blockers: list[str],
        stages: list[dict[str, Any]],
        execution_run_id: int | None = None,
        error_code: str = "",
        error_msg: str = "",
    ) -> dict[str, Any]:
        run = _paper_run(paper_result)
        run_id = execution_run_id or _optional_int(run.get("id"))
        step_durations = _step_durations(self.store, run_id)
        selected_opportunity = (
            self.store.get_opportunity(int(selected_opportunity["id"]))
            if selected_opportunity and selected_opportunity.get("id")
            else selected_opportunity
        )
        selected_route = (
            self.store.get_route(int(selected_route["id"]))
            if selected_route and selected_route.get("id")
            else selected_route
        )
        payload = {
            "request": _safe_request_payload(request),
            "provider_results": provider_results,
            "detector_result": detector_result,
            "edge_evaluation": edge_evaluation,
            "precheck": precheck,
            "paper_result": paper_result or {},
            "paper_summary": _paper_summary(paper_result or {}),
            "selected_opportunity": selected_opportunity or {},
            "selected_route": selected_route or {},
            "blockers": list(blockers),
            "stages": stages,
            "stage_status": _stage_status(stages),
            "step_durations_ms": step_durations,
            "simulated_pnl": _simulated_pnl(paper_result or {}, store=self.store),
            **READ_ONLY_SIMULATION_FLAGS,
        }
        final = self.store.update_simulation_run(
            simulation_id,
            status=status,
            opportunity_id=_optional_int((selected_opportunity or {}).get("id")),
            route_id=_optional_int((selected_route or {}).get("id")),
            execution_run_id=run_id,
            error_code=error_code,
            error_msg=error_msg,
            payload=payload,
        )
        safe_payload = final.get("payload") if isinstance(final.get("payload"), dict) else payload
        event_type = "simulation.run.completed" if status in TERMINAL_OK_STATUSES else "simulation.run.failed"
        self.store.append_event(
            event_type=event_type,
            opportunity_id=_optional_int((selected_opportunity or {}).get("id")),
            route_id=_optional_int((selected_route or {}).get("id")),
            run_id=run_id,
            severity="info" if status in TERMINAL_OK_STATUSES else "warning",
            payload={
                "simulation_run_id": simulation_id,
                "status": status,
                "opportunity_id": _optional_int((selected_opportunity or {}).get("id")),
                "route_id": _optional_int((selected_route or {}).get("id")),
                "run_id": run_id,
                "blockers": list(blockers),
                "simulated_pnl": payload["simulated_pnl"],
                **READ_ONLY_SIMULATION_FLAGS,
            },
        )
        return {
            "ok": ok,
            "status": status,
            "simulation_run": final,
            "simulation_run_id": simulation_id,
            "error_code": error_code,
            "error_msg": error_msg,
            "opportunity_id": _optional_int((selected_opportunity or {}).get("id")),
            "route_id": _optional_int((selected_route or {}).get("id")),
            "run_id": run_id,
            **safe_payload,
        }


class _StageTracker:
    def __init__(self, store: ArbitrageStore, *, simulation_id: int) -> None:
        self.store = store
        self.simulation_id = int(simulation_id)
        self.records: list[dict[str, Any]] = []
        self._active_stage: str | None = None
        self._active_started_ms = 0
        self._active_perf = 0.0

    def start(self, stage: str) -> None:
        self._active_stage = str(stage)
        self._active_started_ms = now_ms()
        self._active_perf = time.perf_counter()
        self.store.append_event(
            event_type="simulation.run.stage",
            payload={
                "simulation_run_id": self.simulation_id,
                "stage": self._active_stage,
                "status": "RUNNING",
                **READ_ONLY_SIMULATION_FLAGS,
            },
        )

    def complete(
        self,
        stage: str,
        *,
        blockers: Sequence[str] | None = None,
        summary: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        duration_ms = int((time.perf_counter() - self._active_perf) * 1000.0) if self._active_perf else 0
        record = {
            "stage": str(stage),
            "status": "BLOCKED" if blockers else "COMPLETED",
            "started_at_ms": self._active_started_ms or now_ms(),
            "duration_ms": duration_ms,
            "blockers": list(blockers or []),
            "summary": dict(summary or {}),
        }
        self.records.append(record)
        self.store.append_event(
            event_type="simulation.run.stage",
            severity="warning" if blockers else "info",
            payload={
                "simulation_run_id": self.simulation_id,
                **record,
                **READ_ONLY_SIMULATION_FLAGS,
            },
        )
        self._active_stage = None
        self._active_started_ms = 0
        self._active_perf = 0.0
        return self.records

    def fail_current(self, *, error_code: str) -> None:
        if not self._active_stage:
            return
        duration_ms = int((time.perf_counter() - self._active_perf) * 1000.0) if self._active_perf else 0
        record = {
            "stage": self._active_stage,
            "status": "FAILED",
            "started_at_ms": self._active_started_ms or now_ms(),
            "duration_ms": duration_ms,
            "blockers": [str(error_code)],
            "summary": {"error_code": str(error_code)},
        }
        self.records.append(record)
        self.store.append_event(
            event_type="simulation.run.stage",
            severity="warning",
            payload={
                "simulation_run_id": self.simulation_id,
                **record,
                **READ_ONLY_SIMULATION_FLAGS,
            },
        )
        self._active_stage = None


def _provider_jobs_from_request(
    request: Mapping[str, Any],
    default_jobs: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    raw_jobs = request.get("jobs")
    if isinstance(raw_jobs, Sequence) and not isinstance(raw_jobs, (str, bytes, bytearray)):
        inline_jobs = [dict(job) for job in raw_jobs if isinstance(job, Mapping)]
        jobs = inline_jobs if inline_jobs else ([dict(job) for job in default_jobs] if _bounded_live_collect_requested(request) else [])
    elif _bounded_live_collect_requested(request):
        jobs = [dict(job) for job in default_jobs]
    else:
        jobs = []

    selected_ids = {
        str(item)
        for item in _as_sequence(request.get("provider_job_ids") or request.get("job_ids"))
        if str(item).strip()
    }
    out: list[dict[str, Any]] = []
    for index, job in enumerate(jobs):
        if not _job_enabled(job):
            continue
        job_id = _job_identifier(job, index)
        if selected_ids and job_id not in selected_ids:
            continue
        item = dict(job)
        item["job_id"] = job_id
        out.append(item)
    return out


def _safe_request_payload(request: Mapping[str, Any]) -> dict[str, Any]:
    safe = redact_provider_payload(dict(request))
    raw_jobs = request.get("jobs")
    if isinstance(raw_jobs, Sequence) and not isinstance(raw_jobs, (str, bytes, bytearray)):
        safe["jobs"] = [
            _safe_provider_job_for_echo(job)
            for job in raw_jobs
            if isinstance(job, Mapping)
        ]
    return safe


def _safe_provider_job_for_echo(job: Mapping[str, Any]) -> dict[str, Any]:
    raw = dict(job)
    return redact_provider_payload({key: raw[key] for key in SIMULATION_REQUEST_JOB_FIELDS if key in raw})


def _job_identifier(job: Mapping[str, Any], index: int) -> str:
    explicit = job.get("job_id") or job.get("id") or job.get("provider_job_id")
    if explicit not in (None, ""):
        return str(explicit)
    return ":".join(
        (
            str(job.get("provider_key") or f"provider_{index}"),
            str(job.get("capability") or "capability"),
            str(job.get("scope_key") or "default"),
        )
    )


def _bounded_live_collect_requested(request: Mapping[str, Any]) -> bool:
    return any(
        _truthy(request.get(key))
        for key in ("bounded_live_collect", "live_collect", "run_live_collect", "collect_live")
    )


def _job_enabled(job: Mapping[str, Any]) -> bool:
    raw = job.get("enabled", True)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return int(raw) != 0
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}
    return True


def _provider_result_blockers(results: Sequence[Mapping[str, Any]]) -> list[str]:
    blockers: list[str] = []
    for result in results:
        status = str(result.get("status") or "").upper()
        if status in PROVIDER_OK_STATUSES:
            continue
        error_code = str(result.get("error_code") or "provider_result_degraded")
        provider_key = str(result.get("provider_key") or result.get("requested_provider_key") or "provider")
        blockers.append(f"{provider_key}:{error_code}")
    return blockers


def _no_opportunity_blockers(request: Mapping[str, Any], *, stamp: int) -> list[str]:
    if not _request_has_jobs(request):
        return []
    payloads = [job.get("payload") for job in _as_sequence(request.get("jobs")) if isinstance(job, Mapping)]
    if any(_payload_marked_stale(payload) for payload in payloads):
        return ["stale_observations_blocked"]
    observed_values = [_payload_observed_at_ms(payload) for payload in payloads]
    observed_values = [value for value in observed_values if value is not None]
    if observed_values and max(observed_values) + 30_000 < int(stamp):
        return ["stale_observations_blocked"]
    return []


def _request_has_jobs(request: Mapping[str, Any]) -> bool:
    return bool(_as_sequence(request.get("jobs")) or _as_sequence(request.get("provider_job_ids") or request.get("job_ids")))


def _simulation_route_payload(
    existing_payload: Mapping[str, Any],
    *,
    route_type: str,
    request: Mapping[str, Any],
    stamp: int,
    fresh_until_ms: int,
) -> dict[str, Any]:
    payload = {
        "simulation_edge_evidence": {
            "source": "no_funds_simulation",
            "observed_at_ms": int(stamp),
            **READ_ONLY_SIMULATION_FLAGS,
        },
        "evaluated_at_ms": int(stamp),
        "gas_bps": _float_or_default(request.get("gas_bps"), 0.0),
        "gas_fresh_until_ms": int(fresh_until_ms),
        "swap_fee_bps": _float_or_default(request.get("swap_fee_bps"), 0.0),
        "swap_fee_fresh_until_ms": int(fresh_until_ms),
        "slippage_bps": _float_or_default(request.get("slippage_bps"), 0.0),
        "slippage_fresh_until_ms": int(fresh_until_ms),
        "latency_haircut_bps": _float_or_default(request.get("latency_haircut_bps"), 0.0),
        "latency_fresh_until_ms": int(fresh_until_ms),
        "status_observed_at_ms": int(stamp),
        "status_fresh_until_ms": int(fresh_until_ms),
        **READ_ONLY_SIMULATION_FLAGS,
    }
    deposit_status = _requested_status(request, "deposit_status")
    bridge_status = _requested_status(request, "bridge_status")
    if route_type in {"direct_cex_sell", "bridge_cex_sell"}:
        if deposit_status:
            payload["deposit_status"] = deposit_status
        elif not _has_any(existing_payload, ("deposit_status", "cex_deposit_status", "cex_deposit")):
            payload["deposit_status"] = "PASS"
            payload["deposit_status_simulated"] = True
    if route_type in {"bridge_dex_sell", "bridge_cex_sell"}:
        if bridge_status:
            payload["bridge_status"] = bridge_status
        elif not _has_any(existing_payload, ("bridge_status", "bridge_availability", "bridge")):
            payload["bridge_status"] = "PASS"
            payload["bridge_status_simulated"] = True
    return payload


def _ensure_simulation_exit_quote(
    store: ArbitrageStore,
    route: Mapping[str, Any],
    *,
    route_type: str,
    request: Mapping[str, Any],
    stamp: int,
    fresh_until_ms: int,
) -> None:
    if route_type not in {"same_dex_sell", "bridge_dex_sell"}:
        return
    route_id = int(route["id"])
    latest_quote = store.get_latest_route_quote(route_id, leg_type="exit")
    if latest_quote:
        latest_expires = _optional_int(latest_quote.get("expires_at_ms")) or 0
        latest_stale = bool(latest_quote.get("stale"))
        if not latest_stale and latest_expires >= int(stamp):
            return
        _record_simulation_exit_quote(
            store,
            route,
            route_type=route_type,
            request=request,
            stamp=stamp,
            fresh_until_ms=fresh_until_ms,
            source_quote=latest_quote,
        )
        return
    _record_simulation_exit_quote(
        store,
        route,
        route_type=route_type,
        request=request,
        stamp=stamp,
        fresh_until_ms=fresh_until_ms,
    )


def _record_simulation_exit_quote(
    store: ArbitrageStore,
    route: Mapping[str, Any],
    *,
    route_type: str,
    request: Mapping[str, Any],
    stamp: int,
    fresh_until_ms: int,
    source_quote: Mapping[str, Any] | None = None,
) -> None:
    route_id = int(route["id"])
    notional = _float_or_default(request.get("trade_amount_krw"), 100_000.0)
    edge_bps = _float_or_default(route.get("edge_expected_bps"), 0.0)
    expected = _optional_float((source_quote or {}).get("amount_out_expected_krw"))
    if expected is None:
        expected = notional * (1.0 + edge_bps / 10_000.0)
    minimum = _optional_float((source_quote or {}).get("amount_out_min_krw"))
    if minimum is None:
        minimum = expected
    store.record_route_quote(
        route_id=route_id,
        leg_type="exit",
        source="no_funds_simulation",
        destination=str((source_quote or {}).get("destination") or route.get("route_type") or route_type),
        amount_in_raw=str((source_quote or {}).get("amount_in_raw") or "1"),
        amount_in_value_krw=_optional_float((source_quote or {}).get("amount_in_value_krw")) or notional,
        amount_out_expected_krw=expected,
        amount_out_min_krw=minimum,
        gas_krw=_optional_float((source_quote or {}).get("gas_krw")) or 0.0,
        fee_krw=_optional_float((source_quote or {}).get("fee_krw")) or 0.0,
        price_impact_bps=_optional_float((source_quote or {}).get("price_impact_bps")) or 0.0,
        observed_at_ms=int(stamp),
        expires_at_ms=int(fresh_until_ms),
        payload={
            **READ_ONLY_SIMULATION_FLAGS,
            "synthetic_quote": True,
            "source_quote_id": _optional_int((source_quote or {}).get("id")),
            "source_quote_preserved": bool(source_quote),
        },
    )


def _ensure_simulation_buy_tick(store: ArbitrageStore, route: Mapping[str, Any], *, stamp: int) -> None:
    buy_market_id = _optional_int(route.get("buy_market_id"))
    if buy_market_id is None:
        return
    with store.conn() as conn:
        tick = conn.execute(
            """
            SELECT *
            FROM arb_market_ticks
            WHERE market_id = ?
              AND observed_at_ms <= ?
            ORDER BY observed_at_ms DESC, id DESC
            LIMIT 1
            """,
            (int(buy_market_id), int(stamp)),
        ).fetchone()
    if not tick:
        return
    if bool(tick["stale"]):
        return
    store.record_market_tick(
        market_id=int(buy_market_id),
        source="no_funds_simulation",
        observed_at_ms=int(stamp),
        raw_price=_optional_float(tick["raw_price"]),
        price_usd=_optional_float(tick["price_usd"]),
        price_krw=_optional_float(tick["price_krw"]),
        best_bid=_optional_float(tick["best_bid"]),
        best_ask=_optional_float(tick["best_ask"]),
        liquidity_usd=_optional_float(tick["liquidity_usd"]),
        volume_24h=_optional_float(tick["volume_24h"]),
        stale=False,
        payload=READ_ONLY_SIMULATION_FLAGS,
    )


def _extend_quote_freshness_for_wall_clock(store: ArbitrageStore, route_id: int, *, ttl_ms: int) -> None:
    # Engine gates use wall-clock time, while tests and replay simulations often
    # evaluate historical observations with an injected now_ms.
    fresh_until_ms = now_ms() + int(ttl_ms)
    with store.conn() as conn:
        conn.execute(
            "UPDATE arb_routes SET quote_fresh_until_ms = ?, updated_at_ms = ? WHERE id = ?",
            (fresh_until_ms, now_ms(), int(route_id)),
        )


def _merge_route_payload(store: ArbitrageStore, route_id: int, payload: Mapping[str, Any]) -> None:
    route = store.get_route(route_id) or {}
    existing = _mapping_or_none(route.get("payload")) or {}
    merged = {**existing, **dict(payload)}
    with store.conn() as conn:
        conn.execute(
            "UPDATE arb_routes SET payload_json = ?, updated_at_ms = ? WHERE id = ?",
            (json.dumps(merged, ensure_ascii=False, sort_keys=True), now_ms(), int(route_id)),
        )


def _precheck_checks(edge_evaluation: Mapping[str, Any] | None, *, blockers: list[str]) -> list[dict[str, Any]]:
    if blockers:
        return [
            {
                "check_name": "route_edge",
                "status": "BLOCK",
                "error_code": blockers[0],
                "details": {"blockers": blockers, **READ_ONLY_SIMULATION_FLAGS},
            }
        ]
    edge = dict(edge_evaluation or {})
    return [
        {
            "check_name": "route_edge",
            "status": "PASS",
            "details": {
                "edge_worst_verified": bool(edge.get("edge_worst_verified")),
                "edge_worst_bps": edge.get("edge_worst_bps"),
                **READ_ONLY_SIMULATION_FLAGS,
            },
        },
        {"check_name": "stale_data", "status": "PASS", "details": READ_ONLY_SIMULATION_FLAGS},
        {"check_name": "wallet_permission", "status": "PASS", "details": READ_ONLY_SIMULATION_FLAGS},
    ]


def _edge_blockers(edge_evaluation: Mapping[str, Any] | None) -> list[str]:
    edge = dict(edge_evaluation or {})
    blockers = [str(item) for item in edge.get("blocker_reasons") or []]
    if edge and not bool(edge.get("edge_worst_verified")) and not blockers:
        blockers.append("edge_worst_unverified")
    return _unique(blockers)


def _precheck_blockers(precheck: Mapping[str, Any] | None, route: Mapping[str, Any] | None) -> list[str]:
    blockers: list[str] = []
    if not precheck:
        blockers.append("precheck_failed")
    elif "ok" in precheck and not bool(precheck.get("ok")):
        blockers.append(str((precheck or {}).get("error_code") or "precheck_failed"))
    elif str(precheck.get("status") or "").upper() not in {"PASS"}:
        blockers.append(f"precheck_{str(precheck.get('status') or 'failed').lower()}")
    route_blockers = (route or {}).get("blocker_reasons")
    if isinstance(route_blockers, Sequence) and not isinstance(route_blockers, (str, bytes, bytearray)):
        blockers.extend(str(item) for item in route_blockers)
    return _unique(blockers)


def _paper_blockers(result: Mapping[str, Any] | None) -> list[str]:
    if result and result.get("ok"):
        return []
    blockers = [str(item) for item in (result or {}).get("blockers") or []]
    run = _paper_run(result or {})
    if run.get("error_code"):
        blockers.extend(str(item) for item in str(run.get("error_code")).split(",") if item)
    if (result or {}).get("error_code"):
        blockers.append(str((result or {}).get("error_code")))
    return _unique(blockers or ["paper_execution_blocked"])


def _paper_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    run = _paper_run(result)
    return {
        "ok": bool(result.get("ok")),
        "run_id": run.get("id"),
        "status": run.get("status"),
        "mode": run.get("mode"),
        **READ_ONLY_SIMULATION_FLAGS,
    }


def _paper_run(result: Mapping[str, Any] | None) -> dict[str, Any]:
    run = (result or {}).get("run")
    return dict(run) if isinstance(run, Mapping) else {}


def _simulated_pnl(result: Mapping[str, Any], *, store: ArbitrageStore | None = None) -> dict[str, Any]:
    run = _paper_run(result)
    realized = 0.0
    if store is not None and run.get("id"):
        positions = store.fetch_positions(run_id=int(run["id"]))
        for position in positions:
            try:
                realized += float(position.get("realized_pnl_krw") or 0.0)
            except (TypeError, ValueError):
                continue
    return {
        "status": run.get("status"),
        "realized_pnl_krw": realized,
        "paper_only": True,
        **READ_ONLY_SIMULATION_FLAGS,
    }


def _step_durations(store: ArbitrageStore, run_id: int | None) -> dict[str, int]:
    if run_id is None:
        return {}
    durations: dict[str, int] = {}
    for step in store.fetch_execution_steps(run_id):
        duration = _optional_int(step.get("duration_ms"))
        if duration is not None:
            durations[str(step.get("step_key") or "")] = int(duration)
    return durations


def _stage_status(stages: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    return {str(stage.get("stage")): str(stage.get("status") or "") for stage in stages}


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _float_or_default(value: Any, default: float) -> float:
    parsed = _optional_float(value)
    return float(default if parsed is None else parsed)


def _optional_int(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mapping_or_none(value: Any) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _as_sequence(value: Any) -> list[Any]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return list(value)
    return []


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enabled"}
    return False


def _has_any(payload: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
    return any(key in payload and payload.get(key) not in (None, "") for key in keys)


def _requested_status(request: Mapping[str, Any], key: str) -> str:
    status = request.get(key)
    if status is None and isinstance(request.get("simulated_provider_status"), Mapping):
        status = request["simulated_provider_status"].get(key)
    return str(status or "").strip().upper()


def _status_freshness_allowed(
    payload: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    request_key: str,
    payload_keys: tuple[str, ...],
) -> bool:
    requested = _requested_status(request, request_key)
    if requested:
        return requested in {"PASS", "OK", "OPEN", "ENABLED", "AVAILABLE", "SUCCESS", "DONE", "READY", "VERIFIED"}
    for key in payload_keys:
        if key not in payload:
            continue
        status = _status_text(payload.get(key))
        if status:
            return status in {"PASS", "OK", "OPEN", "ENABLED", "AVAILABLE", "SUCCESS", "DONE", "READY", "VERIFIED"}
    return True


def _status_text(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("status", value.get("state", value.get("result")))
    return str(value or "").strip().upper()


def _payload_observed_at_ms(payload: Any) -> int | None:
    if isinstance(payload, Mapping):
        for key in ("observed_at_ms", "observedAt", "timestamp", "ts"):
            parsed = _optional_int(payload.get(key))
            if parsed is not None:
                return parsed
        values = [_payload_observed_at_ms(item) for item in payload.values()]
        values = [value for value in values if value is not None]
        return max(values) if values else None
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        values = [_payload_observed_at_ms(item) for item in payload]
        values = [value for value in values if value is not None]
        return max(values) if values else None
    return None


def _payload_marked_stale(payload: Any) -> bool:
    if isinstance(payload, Mapping):
        status = str(payload.get("status") or payload.get("state") or "").strip().lower()
        if status in {"stale", "expired"}:
            return True
        for key in ("stale", "is_stale", "expired", "isExpired"):
            value = payload.get(key)
            if isinstance(value, bool) and value:
                return True
            if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "stale", "expired"}:
                return True
        return any(_payload_marked_stale(item) for item in payload.values())
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        return any(_payload_marked_stale(item) for item in payload)
    return False


def _unique(values: Sequence[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        text = str(value)
        if text and text not in out:
            out.append(text)
    return out
