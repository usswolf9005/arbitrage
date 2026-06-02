from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from .collectors.base import STATUS_DEGRADED, STATUS_OK, CollectorResult, redact_provider_payload, redact_provider_text
from .collectors.cex import ingest_cex_orderbook_fixture
from .collectors.dex import ingest_dex_fixture
from .collectors.fx import ingest_fx_fixture
from .collectors.rpc import ingest_rpc_freshness_fixture
from .providers.base import (
    CAPABILITY_CEX_ORDERBOOK,
    CAPABILITY_COIN_PRICE,
    CAPABILITY_DEX_POOL,
    CAPABILITY_DEX_PAIR_SEARCH,
    CAPABILITY_DEX_POOL_PRICE,
    CAPABILITY_FX_RATE,
    CAPABILITY_KRW_ORDERBOOK,
    CAPABILITY_RPC_FRESHNESS,
    CAPABILITY_RPC_BLOCK_FRESHNESS,
    normalize_capability,
)
from .providers.http_adapters import ReadOnlyHttpAdapterCatalog
from .providers.registry import ProviderRegistry
from .store import ArbitrageStore, now_ms as store_now_ms


READ_ONLY_CAPABILITIES = {
    CAPABILITY_DEX_POOL,
    CAPABILITY_DEX_POOL_PRICE,
    CAPABILITY_DEX_PAIR_SEARCH,
    CAPABILITY_COIN_PRICE,
    CAPABILITY_CEX_ORDERBOOK,
    CAPABILITY_KRW_ORDERBOOK,
    CAPABILITY_FX_RATE,
    CAPABILITY_RPC_FRESHNESS,
    CAPABILITY_RPC_BLOCK_FRESHNESS,
}
DEFAULT_PROVIDER_JOB_CAPABILITIES = {
    CAPABILITY_DEX_POOL,
    CAPABILITY_CEX_ORDERBOOK,
    CAPABILITY_KRW_ORDERBOOK,
    CAPABILITY_FX_RATE,
    CAPABILITY_RPC_FRESHNESS,
}

PRIVATE_CAPABILITY_PREFIXES = ("swap_build", "bridge_build", "cex_order_submit", "withdraw", "sign")
ProviderFetcher = Callable[[Mapping[str, Any]], Any]


@dataclass(frozen=True, slots=True)
class ProviderJobResult:
    provider_key: str
    capability: str
    scope_key: str
    status: str
    inserted_count: int = 0
    deadletter_count: int = 0
    cursor_before: str = ""
    cursor_after: str = ""
    latency_ms: float | None = None
    error_code: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class LiveProviderJobRunner:
    """Read-only provider job runner.

    Network transport is injected through fetchers. That keeps this layer safe by
    default and makes API-key providers easy to add without changing storage,
    detector, execution, or UI contracts.
    """

    def __init__(
        self,
        store: ArbitrageStore,
        *,
        fetchers: Mapping[str, ProviderFetcher] | None = None,
        registry: ProviderRegistry | None = None,
        http_adapters: ReadOnlyHttpAdapterCatalog | None = None,
        default_jobs: Sequence[Mapping[str, Any]] | None = None,
    ) -> None:
        self.store = store
        self.fetchers = dict(fetchers or {})
        self.registry = registry or ProviderRegistry()
        self.http_adapters = http_adapters
        self._default_jobs = tuple(dict(job) for job in default_jobs or ())

    def run_once(self, jobs: Sequence[Mapping[str, Any]], *, now_ms: int | None = None) -> list[ProviderJobResult]:
        results: list[ProviderJobResult] = []
        for job in jobs:
            if not _job_enabled(job):
                continue
            results.append(self.run_job(job, now_ms=now_ms))
        return results

    def run_job(self, job: Mapping[str, Any], *, now_ms: int | None = None) -> ProviderJobResult:
        provider_key = str(job.get("provider_key") or "").strip()
        capability = str(job.get("capability") or "").strip()
        normalized_capability = normalize_capability(capability)
        scope_key = str(job.get("scope_key") or capability or "default").strip()
        if not _job_enabled(job):
            return ProviderJobResult(
                provider_key=provider_key or "provider_disabled",
                capability=normalized_capability or capability,
                scope_key=scope_key,
                status="DISABLED",
                error_code="job_disabled",
            )
        if not provider_key:
            raise ValueError("provider_key_required")
        if (
            capability not in READ_ONLY_CAPABILITIES
            and normalized_capability not in READ_ONLY_CAPABILITIES
        ) or _private_capability(capability):
            return self._record_failure(
                provider_key=provider_key,
                capability=capability,
                scope_key=scope_key,
                cursor_before=self.store.get_collect_cursor(provider_key, scope_key),
                error_code="capability_not_read_only",
                retryable=False,
                raw_payload=_failure_payload(
                    job,
                    provider_key=provider_key,
                    capability=capability,
                    error_code="capability_not_read_only",
                ),
            )

        self.store.append_event(
            event_type="provider.job.started",
            payload={"provider_key": provider_key, "capability": capability, "scope_key": scope_key},
        )
        start = time.perf_counter()
        cursor_before = self.store.get_collect_cursor(provider_key, scope_key)
        try:
            payload = job["payload"] if "payload" in job else self._fetch_payload(provider_key, job)
            if payload is None:
                return self._record_failure(
                    provider_key=provider_key,
                    capability=capability,
                    scope_key=scope_key,
                    cursor_before=cursor_before,
                    error_code="provider_result_null",
                    retryable=True,
                    raw_payload=_failure_payload(
                        job,
                        provider_key=provider_key,
                        capability=capability,
                        error_code="provider_result_null",
                    ),
                )
            stale_source = _payload_is_stale(payload)
            result = self._ingest(
                provider_key=provider_key,
                capability=capability,
                scope_key=scope_key,
                payload=payload,
                now_ms=now_ms,
                stale_evidence=stale_source,
            )
            latency_ms = (time.perf_counter() - start) * 1000.0
            if result.status == STATUS_OK:
                self._connect_freshness(job, payload=payload, capability=capability, now_ms=now_ms)
            error_code = "" if result.status == STATUS_OK else self._latest_collect_error_code(provider_key, scope_key)
            if result.status != STATUS_OK and error_code != "provider_result_stale":
                self._append_provider_job_dead_letter(
                    job,
                    provider_key=provider_key,
                    capability=capability,
                    scope_key=scope_key,
                    error_code=error_code,
                    payload=payload,
                    stale_source=stale_source,
                    retryable=True,
                )
            out = ProviderJobResult(
                provider_key=provider_key,
                capability=capability,
                scope_key=scope_key,
                status=result.status,
                inserted_count=result.inserted_count,
                deadletter_count=result.deadletter_count,
                cursor_before=result.cursor_before,
                cursor_after=result.cursor_after,
                latency_ms=latency_ms,
                error_code=error_code,
            )
            self.store.append_event(
                event_type="provider.job.completed" if result.status == STATUS_OK else "provider.job.failed",
                severity="info" if result.status == STATUS_OK else "warning",
                payload=out.to_dict(),
            )
            return out
        except TimeoutError:
            return self._record_failure(
                provider_key=provider_key,
                capability=capability,
                scope_key=scope_key,
                cursor_before=cursor_before,
                error_code="provider_timeout",
                retryable=True,
                raw_payload=_failure_payload(
                    job,
                    provider_key=provider_key,
                    capability=capability,
                    error_code="provider_timeout",
                ),
            )
        except Exception as exc:
            return self._record_failure(
                provider_key=provider_key,
                capability=capability,
                scope_key=scope_key,
                cursor_before=cursor_before,
                error_code="provider_fetch_failed",
                retryable=True,
                raw_payload=_failure_payload(
                    job,
                    provider_key=provider_key,
                    capability=capability,
                    error_code="provider_fetch_failed",
                    extra={"error": redact_provider_text(str(exc))},
                ),
            )

    def default_provider_jobs(self) -> list[dict[str, Any]]:
        if self._default_jobs:
            return [dict(job) for job in self._default_jobs]
        jobs: list[dict[str, Any]] = []
        for capability in sorted(DEFAULT_PROVIDER_JOB_CAPABILITIES):
            providers = self.registry.providers_for(capability)
            for provider in providers:
                jobs.append(
                    {
                        "provider_key": provider.provider_key,
                        "capability": capability,
                        "scope_key": capability,
                        "enabled": True,
                        "display_name": provider.display_name,
                    }
                )
        if jobs:
            return jobs
        return [
            {"provider_key": status.provider_key, "capability": "", "scope_key": "", "enabled": status.enabled, "reason": status.reason}
            for status in self.registry.all_statuses()
        ]

    def _fetch_payload(self, provider_key: str, job: Mapping[str, Any]) -> Any:
        capability = str(job.get("capability") or "").strip()
        normalized_capability = normalize_capability(capability)
        fetcher = (
            self.fetchers.get(f"{provider_key}:{capability}")
            or self.fetchers.get(f"{provider_key}:{normalized_capability}")
            or self.fetchers.get(provider_key)
        )
        if fetcher is not None:
            return fetcher(job)
        if self.http_adapters is not None:
            return self.http_adapters.fetch_payload(job)
        raise RuntimeError("provider_fetcher_missing")

    def _ingest(
        self,
        *,
        provider_key: str,
        capability: str,
        scope_key: str,
        payload: Any,
        now_ms: int | None,
        stale_evidence: bool = False,
    ) -> CollectorResult:
        normalized_capability = normalize_capability(capability)
        if normalized_capability == CAPABILITY_DEX_POOL or capability in {
            CAPABILITY_DEX_PAIR_SEARCH,
            CAPABILITY_COIN_PRICE,
        }:
            return ingest_dex_fixture(
                self.store,
                payload,
                provider_key=provider_key,
                scope_key=scope_key,
                now_ms=now_ms,
                stale_evidence=stale_evidence,
            )
        if capability in {CAPABILITY_CEX_ORDERBOOK, CAPABILITY_KRW_ORDERBOOK}:
            return ingest_cex_orderbook_fixture(
                self.store,
                payload,
                provider_key=provider_key,
                scope_key=scope_key,
                now_ms=now_ms,
                stale_evidence=stale_evidence,
            )
        if capability == CAPABILITY_FX_RATE:
            return ingest_fx_fixture(
                self.store,
                payload,
                provider_key=provider_key,
                scope_key=scope_key,
                now_ms=now_ms,
                stale_evidence=stale_evidence,
            )
        if normalized_capability == CAPABILITY_RPC_FRESHNESS:
            return ingest_rpc_freshness_fixture(self.store, payload, provider_key=provider_key, scope_key=scope_key, now_ms=now_ms)
        raise ValueError(f"unsupported_read_only_capability:{capability}")

    def _record_failure(
        self,
        *,
        provider_key: str,
        capability: str,
        scope_key: str,
        cursor_before: str,
        error_code: str,
        retryable: bool,
        raw_payload: dict[str, Any],
    ) -> ProviderJobResult:
        self.store.record_collect_failure(
            provider_key=provider_key,
            scope_key=scope_key,
            cursor_before=cursor_before,
            error_code=error_code,
            retryable=retryable,
            raw_payload=raw_payload,
        )
        out = ProviderJobResult(
            provider_key=provider_key,
            capability=capability,
            scope_key=scope_key,
            status=STATUS_DEGRADED,
            cursor_before=cursor_before,
            cursor_after=cursor_before,
            deadletter_count=1,
            error_code=error_code,
        )
        self.store.append_event(event_type="provider.job.failed", severity="warning", payload=out.to_dict())
        return out

    def _append_provider_job_dead_letter(
        self,
        job: Mapping[str, Any],
        *,
        provider_key: str,
        capability: str,
        scope_key: str,
        error_code: str,
        payload: Any,
        stale_source: bool,
        retryable: bool,
    ) -> None:
        cursor_before = self.store.get_collect_cursor(provider_key, scope_key)
        self.store.append_dead_letter(
            reason="provider_job_failed",
            deadletter_key=":".join(
                (
                    "provider_job_failed",
                    provider_key,
                    scope_key,
                    normalize_capability(capability) or capability,
                    error_code,
                    cursor_before,
                )
            ),
            error_code=error_code,
            retryable=retryable,
            payload={
                "provider": redact_provider_text(provider_key),
                "provider_key": redact_provider_text(provider_key),
                "capability": normalize_capability(capability) or capability,
                "scope_key": scope_key,
                "stale_source": bool(stale_source),
                "retry_count": 0,
                "payload_summary": _payload_summary(payload),
                "job": redact_provider_payload(dict(job)),
            },
        )

    def _connect_freshness(
        self,
        job: Mapping[str, Any],
        *,
        payload: Any,
        capability: str,
        now_ms: int | None,
    ) -> None:
        if normalize_capability(capability) != CAPABILITY_RPC_FRESHNESS:
            return
        ttl_ms = int(job.get("route_freshness_ttl_ms") or 30_000)
        observed_at_ms = _payload_observed_at_ms(payload) or int(now_ms or store_now_ms())
        fresh_until_ms = observed_at_ms + ttl_ms
        route_id = _optional_int(job.get("route_id"))
        route_ids = [route_id] if route_id is not None else self._route_ids_for_scope(str(job.get("scope_key") or ""))
        for item in route_ids:
            self.store.set_route_freshness(int(item), {"rpc_block": fresh_until_ms, "rpc_freshness": fresh_until_ms})

    def _route_ids_for_scope(self, scope_key: str) -> list[int]:
        normalized_scope = scope_key.strip().upper()
        with self.store.conn() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT r.id
                FROM arb_routes r
                JOIN arb_markets bm ON bm.id = r.buy_market_id
                JOIN arb_markets sm ON sm.id = r.sell_market_id
                WHERE ? = ''
                   OR UPPER(bm.chain_code) = ?
                   OR UPPER(sm.chain_code) = ?
                """,
                (normalized_scope, normalized_scope, normalized_scope),
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def _latest_collect_error_code(self, provider_key: str, scope_key: str) -> str:
        for row in reversed(self.store.fetch_dead_letters()):
            payload = row.get("payload") if isinstance(row.get("payload"), Mapping) else {}
            if payload.get("provider_key") == provider_key and payload.get("scope_key") == scope_key:
                return str(row.get("error_code") or payload.get("error_code") or "provider_result_degraded")
        return "provider_result_degraded"


def _payload_observed_at_ms(payload: Any) -> int | None:
    if not isinstance(payload, Mapping):
        return None
    for key in ("observed_at_ms", "observedAt", "timestamp", "ts"):
        value = payload.get(key)
        parsed = _optional_int(value)
        if parsed is not None:
            return parsed
    result = payload.get("result")
    if isinstance(result, Mapping):
        for key in ("observed_at_ms", "observedAt", "timestamp", "ts"):
            parsed = _optional_int(result.get(key))
            if parsed is not None:
                return parsed
    return None


def _optional_int(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _private_capability(capability: str) -> bool:
    lowered = capability.lower()
    return any(prefix in lowered for prefix in PRIVATE_CAPABILITY_PREFIXES)


def _job_enabled(job: Mapping[str, Any]) -> bool:
    raw = job.get("enabled", True)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return int(raw) != 0
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "no", "off", "disabled"}
    return True


def _failure_payload(
    job: Mapping[str, Any],
    *,
    provider_key: str,
    capability: str,
    error_code: str,
    payload: Any = None,
    stale_source: bool = False,
    retry_count: int = 0,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "provider": redact_provider_text(provider_key),
        "provider_key": redact_provider_text(provider_key),
        "capability": normalize_capability(capability) or capability,
        "error_code": str(error_code),
        "stale_source": bool(stale_source),
        "retry_count": max(0, int(retry_count)),
        "payload_summary": _payload_summary(payload),
        "job": redact_provider_payload(dict(job)),
        **dict(extra or {}),
    }


def _payload_summary(payload: Any) -> dict[str, Any]:
    if payload is None:
        return {}
    if isinstance(payload, Mapping):
        summary: dict[str, Any] = {
            "type": "object",
            "keys": sorted(str(key) for key in payload.keys())[:25],
        }
        for key in ("status", "state", "stale", "is_stale", "expired", "observed_at_ms", "observedAt", "timestamp", "ts"):
            if key in payload:
                summary[key] = redact_provider_payload(payload.get(key))
        for key in ("pairs", "orderbooks", "rates", "data"):
            value = payload.get(key)
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                summary[f"{key}_count"] = len(value)
        if "result" in payload:
            result = payload.get("result")
            summary["result_type"] = type(result).__name__
            if isinstance(result, Mapping):
                summary["result_keys"] = sorted(str(key) for key in result.keys())[:25]
        return redact_provider_payload(summary)
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        return {"type": "list", "count": len(payload)}
    return {"type": type(payload).__name__}


def _payload_is_stale(payload: Any) -> bool:
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
        return any(_payload_is_stale(item) for item in payload.values())
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        return any(_payload_is_stale(item) for item in payload)
    return False
