from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .collectors.base import redact_provider_payload
from .engine import ArbitrageEngine
from .live_collectors import PRIVATE_CAPABILITY_PREFIXES, READ_ONLY_CAPABILITIES, LiveProviderJobRunner
from .providers.base import READ_ONLY_HTTP_V1_CAPABILITY_SET, normalize_capability
from .providers.http_adapters import ReadOnlyHttpAdapterCatalog
from .simulation import SimulationRunner
from .store import ArbitrageStore, DEFAULT_DB_PATH

_LOG = logging.getLogger(__name__)

DEFAULT_STATIC_DIR = Path(__file__).resolve().parent / "dist"
PROVIDER_JOBS_ENV = "ARBITRAGE_PROVIDER_JOBS_JSON"
PUBLIC_PROVIDER_JOB_FIELDS = {
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
}


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(int(status))
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    if not raw.strip():
        return {}
    return json.loads(raw)


def encode_sse_event(row: dict[str, Any]) -> str:
    event_type = str(row.get("event_type") or "message")
    seq = int(row.get("seq") or 0)
    data = json.dumps(row, ensure_ascii=False, sort_keys=True)
    return f"id: {seq}\nevent: {event_type}\ndata: {data}\n\n"


def _optional_int_query(qs: dict[str, list[str]], key: str) -> tuple[int | None, str | None]:
    raw = (qs.get(key) or [""])[0]
    if raw == "":
        return None, None
    if not str(raw).isdigit():
        return None, f"invalid_{key}"
    return int(raw), None


def _approval_evidence(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("payload", payload.get("evidence", {}))
    return dict(raw) if isinstance(raw, dict) else {"value": raw}


def _decision_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw = payload.get("decision_payload", payload.get("payload", {}))
    return dict(raw) if isinstance(raw, dict) else {"value": raw}


def _simulation_response(run: dict[str, Any]) -> dict[str, Any]:
    payload = run.get("payload") if isinstance(run.get("payload"), dict) else {}
    return {
        "simulation_run": run,
        "stage_status": payload.get("stage_status", {}),
        "stages": payload.get("stages", []),
        "selected_opportunity": payload.get("selected_opportunity", {}),
        "selected_route": payload.get("selected_route", {}),
        "blockers": payload.get("blockers", []),
        "step_durations_ms": payload.get("step_durations_ms", {}),
        "simulated_pnl": payload.get("simulated_pnl", {}),
        "error_code": run.get("error_code") or payload.get("error_code") or "",
        "error_msg": run.get("error_msg") or payload.get("error_msg") or "",
        "no_real_funds": bool(payload.get("no_real_funds", False)),
        "no_real_submit": bool(payload.get("no_real_submit", False)),
    }


def _provider_job_rows(store: ArbitrageStore) -> list[dict[str, Any]]:
    health_by_provider = {str(row.get("provider_key") or ""): row for row in store.fetch_provider_health()}
    rows: list[dict[str, Any]] = []
    for job in _read_only_provider_runner(store).default_provider_jobs():
        safe_job = _public_provider_job(job)
        provider_key = str(job.get("provider_key") or "")
        health = health_by_provider.get(provider_key, {})
        config_error = _provider_job_config_error(job, require_http_adapter="payload" not in job)
        enabled = _job_enabled(job)
        rows.append(
            {
                **safe_job,
                "status": "DISABLED" if config_error or not enabled else (health.get("status") or "ENABLED"),
                "error_code": config_error or ("job_disabled" if not enabled else (health.get("error_code") or "")),
                "latency_ms": health.get("latency_ms"),
                "cooldown_until_ms": health.get("cooldown_until_ms"),
                "health_payload": health.get("payload") or {},
            }
        )
    return rows


def _public_provider_job(job: dict[str, Any] | Any) -> dict[str, Any]:
    raw = dict(job or {})
    return redact_provider_payload({key: raw[key] for key in PUBLIC_PROVIDER_JOB_FIELDS if key in raw})


def _configured_provider_jobs_from_env() -> list[dict[str, Any]]:
    raw = os.getenv(PROVIDER_JOBS_ENV, "").strip()
    if not raw:
        return []
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        # 잘못된 JSON은 조용히 무시하면 operator가 "no jobs" 원인을 추적할 수 없다.
        _LOG.warning("invalid %s JSON; ignoring configured provider jobs", PROVIDER_JOBS_ENV)
        return []
    if not isinstance(decoded, list):
        return []
    jobs: list[dict[str, Any]] = []
    for item in decoded:
        if not isinstance(item, dict):
            continue
        provider_key = str(item.get("provider_key") or "").strip()
        capability = str(item.get("capability") or "").strip()
        if not provider_key or not capability:
            continue
        jobs.append(dict(item))
    return jobs


def _job_enabled(job: dict[str, Any] | Any) -> bool:
    raw = (dict(job or {})).get("enabled", True)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return int(raw) != 0
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}
    return True


def _private_capability(capability: str) -> bool:
    lowered = str(capability or "").lower()
    return any(prefix in lowered for prefix in PRIVATE_CAPABILITY_PREFIXES)


def _provider_job_config_error(job: dict[str, Any] | Any, *, require_http_adapter: bool = False) -> str:
    raw = dict(job or {})
    if not _job_enabled(raw):
        return ""
    capability = str(raw.get("capability") or "").strip()
    normalized = normalize_capability(capability)
    if (
        capability not in READ_ONLY_CAPABILITIES
        and normalized not in READ_ONLY_CAPABILITIES
    ) or _private_capability(capability) or _private_capability(normalized):
        return "capability_not_read_only"
    if require_http_adapter and normalized not in READ_ONLY_HTTP_V1_CAPABILITY_SET:
        return "http_adapter_not_implemented"
    return ""


def _provider_jobs_config_error(payload: dict[str, Any]) -> str:
    raw_jobs = payload.get("jobs") if _has_inline_provider_jobs(payload) else _configured_provider_jobs_from_env()
    if not isinstance(raw_jobs, list):
        return ""
    for job in raw_jobs:
        if not isinstance(job, dict):
            continue
        require_http_adapter = not _has_inline_provider_jobs(payload) or "payload" not in job
        error = _provider_job_config_error(job, require_http_adapter=require_http_adapter)
        if error:
            return "invalid_provider_jobs_config"
    return ""


def _read_only_provider_runner(store: ArbitrageStore) -> LiveProviderJobRunner:
    return LiveProviderJobRunner(
        store,
        http_adapters=ReadOnlyHttpAdapterCatalog(),
        default_jobs=_configured_provider_jobs_from_env(),
    )


def _snapshot_response(store: ArbitrageStore, engine: ArbitrageEngine, *, selected_opportunity_id: int | None) -> dict[str, Any]:
    snapshot = engine.snapshot(selected_opportunity_id=selected_opportunity_id)
    selected_id = snapshot.get("selected_opportunity_id")
    snapshot["provider_jobs"] = _provider_job_rows(store)
    snapshot["simulation_runs"] = store.list_simulation_runs(
        opportunity_id=int(selected_id) if selected_id else None,
        limit=50,
    )
    return snapshot


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return int(value) == 1
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "ack", "acknowledged"}
    return False


def _explicit_false(value: Any) -> bool:
    if isinstance(value, bool):
        return not value
    if isinstance(value, (int, float)):
        return int(value) == 0
    if isinstance(value, str):
        return value.strip().lower() in {"0", "false", "no", "disabled"}
    return False


def _bounded_live_collect_requested(payload: dict[str, Any]) -> bool:
    return any(_truthy(payload.get(key)) for key in ("bounded_live_collect", "live_collect", "run_live_collect", "collect_live"))


def _has_inline_provider_jobs(payload: dict[str, Any]) -> bool:
    jobs = payload.get("jobs")
    return isinstance(jobs, list) and any(isinstance(job, dict) for job in jobs)


def _live_full_boundary_error(payload: dict[str, Any]) -> str:
    if not _truthy(payload.get("live_full_boundary_ack")):
        return "live_full_boundary_ack_required"
    if not _truthy(payload.get("simulated")):
        return "live_full_simulated_boundary_required"
    if not str(payload.get("provider_boundary") or "").strip():
        return "live_full_provider_boundary_required"
    if not _explicit_false(payload.get("cex_withdrawal_enabled")):
        return "cex_withdrawal_boundary_required"
    return ""


class ArbitrageAPIHandler(BaseHTTPRequestHandler):
    server_version = "ArbitrageAPI/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        if os.getenv("ARBITRAGE_API_LOG_REQUESTS", "0").strip().lower() in {"1", "true", "yes"}:
            super().log_message(format, *args)

    @property
    def engine(self) -> ArbitrageEngine:
        return self.server.engine  # type: ignore[attr-defined]

    @property
    def store(self) -> ArbitrageStore:
        return self.server.store  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/arbitrage/snapshot":
            qs = parse_qs(parsed.query)
            selected_raw = (qs.get("selected_opportunity_id") or [""])[0]
            selected = int(selected_raw) if str(selected_raw).isdigit() else None
            _json_response(self, 200, _snapshot_response(self.store, self.engine, selected_opportunity_id=selected))
            return
        if parsed.path == "/api/arbitrage/stream":
            self._stream_events(parsed.query)
            return
        if parsed.path == "/api/arbitrage/provider-jobs":
            _json_response(self, 200, {"provider_jobs": _provider_job_rows(self.store)})
            return
        simulation = re.fullmatch(r"/api/arbitrage/simulation-runs/(\d+)", parsed.path)
        if simulation:
            run = self.store.get_simulation_run(int(simulation.group(1)))
            if not run:
                _json_response(self, 404, {"error": "simulation_run_not_found"})
                return
            _json_response(self, 200, _simulation_response(run))
            return
        if parsed.path == "/api/arbitrage/approvals":
            qs = parse_qs(parsed.query)
            opportunity_id, error = _optional_int_query(qs, "opportunity_id")
            if error:
                _json_response(self, 400, {"error": error})
                return
            route_id, error = _optional_int_query(qs, "route_id")
            if error:
                _json_response(self, 400, {"error": error})
                return
            status = (qs.get("status") or [""])[0] or None
            _json_response(
                self,
                200,
                {
                    "approvals": self.store.list_operator_approvals(
                        opportunity_id=opportunity_id,
                        route_id=route_id,
                        status=status,
                    ),
                    "summary": self.store.summarize_operator_approvals(
                        opportunity_id=opportunity_id,
                        route_id=route_id,
                    ),
                },
            )
            return
        if parsed.path in {"/health", "/api/arbitrage/health"}:
            _json_response(self, 200, {"ok": True, "service": "arbitrage"})
            return
        if parsed.path == "/api" or parsed.path.startswith("/api/"):
            _json_response(self, 404, {"error": "not_found"})
            return
        if _static_response(self, parsed.path):
            return
        _json_response(self, 404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            payload = _read_json(self)
        except Exception:
            _json_response(self, 400, {"error": "invalid_json"})
            return

        precheck = re.fullmatch(r"/api/arbitrage/opportunities/(\d+)/precheck", parsed.path)
        if precheck:
            opportunity_id = int(precheck.group(1))
            route_id = int(payload.get("route_id") or 0)
            if route_id <= 0:
                _json_response(self, 400, {"error": "route_id_required"})
                return
            route = self.store.get_route(route_id)
            if not route:
                _json_response(self, 404, {"error": "route_not_found"})
                return
            if int(route.get("opportunity_id") or 0) != opportunity_id:
                _json_response(self, 400, {"error": "route_opportunity_mismatch"})
                return
            result = self.engine.run_precheck(
                opportunity_id=opportunity_id,
                route_id=route_id,
                checks=list(payload.get("checks") or []),
            )
            _json_response(self, 200, result)
            return

        if parsed.path == "/api/arbitrage/executions":
            opportunity_id = int(payload.get("opportunity_id") or 0)
            route_id = int(payload.get("route_id") or 0)
            opportunity = self.store.get_opportunity(opportunity_id)
            if not opportunity:
                _json_response(self, 404, {"error": "opportunity_not_found"})
                return
            route = self.store.get_route(route_id)
            if not route:
                _json_response(self, 404, {"error": "route_not_found"})
                return
            if int(route.get("opportunity_id") or 0) != opportunity_id:
                _json_response(self, 400, {"error": "route_opportunity_mismatch"})
                return
            mode = str(payload.get("mode") or "paper").strip() or "paper"
            if mode == "live_full":
                boundary_error = _live_full_boundary_error(payload)
                if boundary_error:
                    _json_response(self, 400, {"error": boundary_error})
                    return
            result = self.engine.start_execution(
                opportunity_id=opportunity_id,
                route_id=route_id,
                mode=mode,
                idempotency_key=str(payload.get("idempotency_key") or f"api:{time.time_ns()}"),
                requested_by=str(payload.get("requested_by") or "api"),
                trade_amount_krw=payload.get("trade_amount_krw"),
                execution_policy=str(payload.get("execution_policy") or ""),
            )
            _json_response(self, 202 if result["ok"] else 409, result)
            return

        decision = re.fullmatch(r"/api/arbitrage/approvals/(\d+)/(approve|reject)", parsed.path)
        if decision:
            final_status = "APPROVED" if decision.group(2) == "approve" else "REJECTED"
            try:
                approval = self.store.decide_operator_approval(
                    int(decision.group(1)),
                    status=final_status,
                    operator=str(payload.get("operator") or payload.get("requested_by") or "api"),
                    decision_payload=_decision_payload(payload),
                )
            except ValueError as exc:
                _json_response(self, 409, {"error": str(exc)})
                return
            if not approval:
                _json_response(self, 404, {"error": "approval_not_found"})
                return
            _json_response(self, 200, {"approval": approval})
            return

        if parsed.path == "/api/arbitrage/approvals":
            opportunity_id = int(payload.get("opportunity_id") or 0)
            route_id = int(payload.get("route_id") or 0)
            run_id_raw = payload.get("run_id")
            run_id = int(run_id_raw) if str(run_id_raw or "").isdigit() else None
            if opportunity_id <= 0:
                _json_response(self, 400, {"error": "opportunity_id_required"})
                return
            if route_id <= 0:
                _json_response(self, 400, {"error": "route_id_required"})
                return
            opportunity = self.store.get_opportunity(opportunity_id)
            if not opportunity:
                _json_response(self, 404, {"error": "opportunity_not_found"})
                return
            route = self.store.get_route(route_id)
            if not route:
                _json_response(self, 404, {"error": "route_not_found"})
                return
            if int(route.get("opportunity_id") or 0) != opportunity_id:
                _json_response(self, 400, {"error": "route_opportunity_mismatch"})
                return
            mode = str(payload.get("mode") or "one_click")
            approval_key = str(
                payload.get("approval_key")
                or f"operator_approval:{opportunity_id}:{route_id}:{run_id or 'none'}:{mode}"
            )
            try:
                approval = dict(
                    self.store.request_operator_approval(
                        approval_key=approval_key,
                        opportunity_id=opportunity_id,
                        route_id=route_id,
                        run_id=run_id,
                        mode=mode,
                        requested_by=str(payload.get("requested_by") or "api"),
                        reason=str(payload.get("reason") or ""),
                        payload=_approval_evidence(payload),
                    )
                )
            except ValueError as exc:
                _json_response(self, 409, {"error": str(exc)})
                return
            created = bool(approval.pop("created", False))
            _json_response(self, 201 if created else 200, {"approval": approval, "existing": not created})
            return

        if parsed.path == "/api/arbitrage/demo/seed":
            _json_response(self, 200, self.engine.seed_demo_sol_opportunity())
            return

        if parsed.path == "/api/arbitrage/simulation-runs":
            if _bounded_live_collect_requested(payload) and not _has_inline_provider_jobs(payload) and not _configured_provider_jobs_from_env():
                _json_response(self, 400, {"error": "provider_jobs_required"})
                return
            config_error = _provider_jobs_config_error(payload)
            if config_error:
                _json_response(self, 400, {"error": config_error})
                return
            result = SimulationRunner(self.store, provider_runner=_read_only_provider_runner(self.store)).start(payload)
            _json_response(self, 202 if result.get("ok") else 409, result)
            return

        abort = re.fullmatch(r"/api/arbitrage/executions/(\d+)/abort", parsed.path)
        if abort:
            result = self.engine.abort_execution(int(abort.group(1)))
            _json_response(self, 200 if result.get("ok", True) else 409, result)
            return

        _json_response(self, 404, {"error": "not_found"})

    def _stream_events(self, query: str) -> None:
        qs = parse_qs(query)
        after_seq_raw = (qs.get("after_seq") or ["0"])[0]
        after_seq = int(after_seq_raw) if str(after_seq_raw).isdigit() else 0
        last_event_id = self.headers.get("Last-Event-ID", "")
        if str(last_event_id).isdigit():
            after_seq = max(after_seq, int(last_event_id))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        last_seq = after_seq
        while True:
            try:
                pending_count = self.store.count_event_log_after(last_seq)
                if pending_count > 500:
                    latest = self.store.latest_event_seq()
                    self.wfile.write(
                        encode_sse_event(
                            {
                                "seq": latest,
                                "event_type": "replay_truncated",
                                "severity": "warning",
                                "payload": {"after_seq": last_seq, "pending_count": pending_count, "snapshot_reload_required": True},
                                "occurred_at_ms": int(time.time() * 1000),
                            }
                        ).encode("utf-8")
                    )
                    last_seq = latest
                    self.wfile.flush()
                    time.sleep(0.5)
                    continue
                events = self.store.fetch_event_log_replay(after_seq=last_seq, limit=500)
                if events:
                    for row in events:
                        self.wfile.write(encode_sse_event(row).encode("utf-8"))
                        last_seq = max(last_seq, int(row.get("seq") or 0))
                else:
                    self.wfile.write(b"event: heartbeat\ndata: {}\n\n")
                self.wfile.flush()
                time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError, OSError, sqlite3.Error):
                return


def _static_response(handler: BaseHTTPRequestHandler, request_path: str) -> bool:
    static_dir = getattr(handler.server, "static_dir", None)  # type: ignore[attr-defined]
    if not static_dir:
        return False
    root = Path(static_dir).resolve()
    if not root.exists():
        return False
    relative = "index.html" if request_path in {"", "/"} else request_path.lstrip("/")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        _json_response(handler, 403, {"error": "static_path_forbidden"})
        return True
    if not candidate.is_file():
        return False
    content = candidate.read_bytes()
    content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
    if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
        content_type = f"{content_type}; charset=utf-8"
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(content)))
    handler.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
    handler.send_header("Pragma", "no-cache")
    handler.send_header("Expires", "0")
    handler.end_headers()
    handler.wfile.write(content)
    return True


def create_server(
    host: str = "127.0.0.1",
    port: int = 8791,
    *,
    store: ArbitrageStore | None = None,
    db_path: str | None = None,
    static_dir: str | os.PathLike[str] | None = None,
) -> ThreadingHTTPServer:
    resolved_store = store or ArbitrageStore(db_path or DEFAULT_DB_PATH)
    resolved_store.init()
    server = ThreadingHTTPServer((host, int(port)), ArbitrageAPIHandler)
    server.daemon_threads = True
    server.store = resolved_store  # type: ignore[attr-defined]
    server.engine = ArbitrageEngine(resolved_store)  # type: ignore[attr-defined]
    server.static_dir = Path(static_dir or os.getenv("ARBITRAGE_API_STATIC_DIR", "") or DEFAULT_STATIC_DIR)  # type: ignore[attr-defined]
    return server


def main() -> None:
    host = os.getenv("ARBITRAGE_API_HOST", "127.0.0.1")
    port = int(os.getenv("ARBITRAGE_API_PORT", "8791"))
    db_path = os.getenv("ARBITRAGE_DB_PATH", DEFAULT_DB_PATH)
    server = create_server(host, port, db_path=db_path)
    print(f"arbitrage api listening on http://{host}:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
