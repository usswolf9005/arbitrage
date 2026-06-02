from __future__ import annotations

import os
import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from .collectors.base import STATUS_OK, redact_provider_payload, redact_provider_text
from .live_collectors import LiveProviderJobRunner, ProviderJobResult, READ_ONLY_CAPABILITIES
from .providers.base import normalize_capability
from .providers.http_adapters import ReadOnlyHttpAdapterCatalog
from .providers.registry import ProviderRegistry
from .store import ArbitrageStore, now_ms as store_now_ms


LIVE_COLLECTORS_ENV = "ARBITRAGE_LIVE_COLLECTORS_ENABLED"
HEALTH_ACTIVE = "ACTIVE"
HEALTH_DEGRADED = "DEGRADED"
HEALTH_DISABLED = "DISABLED"
_PRIVATE_CAPABILITY_FRAGMENTS = ("swap_build", "bridge_build", "cex_order_submit", "withdraw", "sign")


@dataclass(frozen=True, slots=True)
class SchedulerJobResult:
    requested_provider_key: str
    provider_key: str
    capability: str
    scope_key: str
    status: str
    attempts: int = 0
    fallback_used: bool = False
    error_code: str = ""
    disabled_reason: str = ""
    cursor_before: str = ""
    cursor_after: str = ""
    next_due_ms: int | None = None
    provider_results: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SchedulerRunSummary:
    enabled: bool
    results: tuple[SchedulerJobResult, ...] = ()
    skipped_reason: str = ""
    iterations: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "results": [result.to_dict() for result in self.results],
            "skipped_reason": self.skipped_reason,
            "iterations": self.iterations,
        }


class ReadOnlyPollingScheduler:
    """Read-only polling scheduler for live observation collection.

    The scheduler owns timing, retries, fallback, and provider health. The
    runner still owns read-only fetch and ingestion so private execution paths
    remain outside the collection loop.
    """

    def __init__(
        self,
        store: ArbitrageStore,
        *,
        runner: LiveProviderJobRunner | None = None,
        fetchers: Mapping[str, Callable[[Mapping[str, Any]], Any]] | None = None,
        registry: ProviderRegistry | None = None,
        http_adapters: ReadOnlyHttpAdapterCatalog | None = None,
        environ: Mapping[str, str] | None = None,
        enabled: bool | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        random_fn: Callable[[], float] | None = None,
        loop_sleep_ms: int = 250,
    ) -> None:
        self.store = store
        self.environ = environ if environ is not None else os.environ
        self.enabled = _env_enabled(self.environ) if enabled is None else bool(enabled)
        self.registry = registry or (runner.registry if runner is not None else ProviderRegistry())
        self.runner = runner or LiveProviderJobRunner(
            store,
            fetchers=fetchers,
            registry=self.registry,
            http_adapters=http_adapters,
        )
        self.sleep_fn = sleep_fn or time.sleep
        self.random_fn = random_fn or random.random
        self.loop_sleep_ms = max(0, int(loop_sleep_ms))
        self._next_due_ms: dict[str, int] = {}

    def run_once(
        self,
        jobs: Sequence[Mapping[str, Any]] | None = None,
        *,
        now_ms: int | None = None,
        force: bool = False,
    ) -> SchedulerRunSummary:
        if not self.enabled and not force:
            return SchedulerRunSummary(enabled=False, skipped_reason="live_collectors_disabled")

        stamp = int(now_ms if now_ms is not None else store_now_ms())
        raw_jobs = tuple(jobs if jobs is not None else self.runner.default_provider_jobs())
        results: list[SchedulerJobResult] = []
        for raw_job in raw_jobs:
            job = dict(raw_job)
            if not _job_enabled(job):
                continue
            if not self._job_due(job, stamp=stamp, force=force):
                continue
            result = self._run_scheduled_job(job, stamp=stamp)
            results.append(result)
            self._schedule_next_due(job, stamp=stamp)
        return SchedulerRunSummary(enabled=True, results=tuple(results))

    def run_loop(
        self,
        jobs: Sequence[Mapping[str, Any]] | None = None,
        *,
        max_iterations: int = 1,
        stop_after_ms: int | None = None,
    ) -> SchedulerRunSummary:
        if not self.enabled:
            return SchedulerRunSummary(enabled=False, skipped_reason="live_collectors_disabled", iterations=0)
        if max_iterations <= 0:
            return SchedulerRunSummary(enabled=True, results=(), iterations=0)

        started = time.monotonic() * 1000.0
        results: list[SchedulerJobResult] = []
        iterations = 0
        while iterations < max_iterations:
            summary = self.run_once(jobs, force=False)
            results.extend(summary.results)
            iterations += 1
            if stop_after_ms is not None and (time.monotonic() * 1000.0 - started) >= int(stop_after_ms):
                break
            if iterations < max_iterations and self.loop_sleep_ms > 0:
                self.sleep_fn(self.loop_sleep_ms / 1000.0)
        return SchedulerRunSummary(enabled=True, results=tuple(results), iterations=iterations)

    def _run_scheduled_job(self, job: dict[str, Any], *, stamp: int) -> SchedulerJobResult:
        capability = str(job.get("capability") or "").strip()
        normalized_capability = normalize_capability(capability)
        requested_provider_key = str(job.get("provider_key") or "").strip()
        scope_key = str(job.get("scope_key") or normalized_capability or "default").strip()
        if (
            normalized_capability not in READ_ONLY_CAPABILITIES
            or _private_capability(normalized_capability)
            or _private_capability(capability)
        ):
            return self._record_scheduler_block(
                job,
                provider_key=requested_provider_key,
                capability=capability,
                scope_key=scope_key,
                error_code="capability_not_read_only",
            )

        disabled_reason = self._disabled_reason(requested_provider_key) if requested_provider_key else ""
        if disabled_reason:
            self._mark_health(
                requested_provider_key,
                HEALTH_DISABLED,
                capability=normalized_capability,
                scope_key=scope_key,
                reason=disabled_reason,
                payload={"requested_provider_key": requested_provider_key},
            )

        candidates = self._candidate_provider_keys(job, capability=normalized_capability, stamp=stamp)
        if not candidates:
            reason = disabled_reason or "no_healthy_provider"
            provider_key = requested_provider_key or "provider_unavailable"
            self._mark_health(
                provider_key,
                HEALTH_DISABLED if disabled_reason else HEALTH_DEGRADED,
                capability=normalized_capability,
                scope_key=scope_key,
                reason=reason,
                payload={"requested_provider_key": requested_provider_key},
            )
            return SchedulerJobResult(
                requested_provider_key=requested_provider_key,
                provider_key=provider_key,
                capability=normalized_capability,
                scope_key=scope_key,
                status=HEALTH_DISABLED if disabled_reason else HEALTH_DEGRADED,
                disabled_reason=reason if disabled_reason else "",
                error_code="" if disabled_reason else reason,
            )

        max_attempts = max(1, int(job.get("max_attempts") or (int(job.get("max_retries") or 0) + 1)))
        backoff_ms = max(0, int(job.get("backoff_ms") or 0))
        provider_results: list[dict[str, Any]] = []
        attempts = 0
        first_cursor = self.store.get_collect_cursor(candidates[0], scope_key)
        last_result: ProviderJobResult | None = None

        for candidate_idx, provider_key in enumerate(candidates):
            candidate_job = {**job, "provider_key": provider_key, "capability": normalized_capability}
            for attempt_no in range(1, max_attempts + 1):
                attempts += 1
                result = self.runner.run_job(candidate_job, now_ms=stamp)
                last_result = result
                provider_results.append(result.to_dict())
                if result.status == STATUS_OK:
                    self._mark_health(
                        provider_key,
                        HEALTH_ACTIVE,
                        capability=normalized_capability,
                        scope_key=scope_key,
                        reason="",
                        latency_ms=result.latency_ms,
                        payload={
                            "attempt_no": attempt_no,
                            "fallback_used": candidate_idx > 0,
                            "cursor_after": result.cursor_after,
                        },
                    )
                    return SchedulerJobResult(
                        requested_provider_key=requested_provider_key,
                        provider_key=provider_key,
                        capability=normalized_capability,
                        scope_key=scope_key,
                        status=STATUS_OK,
                        attempts=attempts,
                        fallback_used=candidate_idx > 0,
                        cursor_before=result.cursor_before,
                        cursor_after=result.cursor_after,
                        next_due_ms=self._next_due_for(job, stamp=stamp),
                        provider_results=tuple(provider_results),
                    )

                error_code = result.error_code or "provider_result_degraded"
                cooldown_until_ms = stamp + _backoff_for_attempt(backoff_ms, attempt_no)
                self._mark_health(
                    provider_key,
                    HEALTH_DEGRADED,
                    capability=normalized_capability,
                    scope_key=scope_key,
                    reason=error_code,
                    latency_ms=result.latency_ms,
                    cooldown_until_ms=cooldown_until_ms if backoff_ms else None,
                    payload={
                        "attempt_no": attempt_no,
                        "max_attempts": max_attempts,
                        "cursor_before": result.cursor_before,
                        "cursor_after": result.cursor_after,
                    },
                )
                if attempt_no < max_attempts and backoff_ms > 0:
                    self.sleep_fn(_backoff_for_attempt(backoff_ms, attempt_no) / 1000.0)

        exhausted_code = (last_result.error_code if last_result is not None else "") or "provider_retries_exhausted"
        if last_result is not None:
            self.store.append_dead_letter(
                reason="provider_retries_exhausted",
                deadletter_key=(
                    f"provider_retries_exhausted:{last_result.provider_key}:"
                    f"{last_result.scope_key}:{exhausted_code}:{stamp}"
                ),
                error_code=exhausted_code,
                retryable=True,
                payload={
                    "provider": redact_provider_text(last_result.provider_key),
                    "capability": normalized_capability,
                    "scope_key": scope_key,
                    "retry_count": max(0, max_attempts - 1),
                    "stale_source": exhausted_code == "provider_result_stale",
                    "payload_summary": redact_provider_payload({"provider_results": provider_results[-3:]}),
                },
            )
        return SchedulerJobResult(
            requested_provider_key=requested_provider_key,
            provider_key=last_result.provider_key if last_result is not None else (requested_provider_key or ""),
            capability=normalized_capability,
            scope_key=scope_key,
            status=HEALTH_DEGRADED,
            attempts=attempts,
            fallback_used=bool(last_result and last_result.provider_key != requested_provider_key),
            error_code=exhausted_code,
            cursor_before=first_cursor,
            cursor_after=self.store.get_collect_cursor(last_result.provider_key, scope_key) if last_result else first_cursor,
            next_due_ms=self._next_due_for(job, stamp=stamp),
            provider_results=tuple(provider_results),
        )

    def _record_scheduler_block(
        self,
        job: Mapping[str, Any],
        *,
        provider_key: str,
        capability: str,
        scope_key: str,
        error_code: str,
    ) -> SchedulerJobResult:
        cursor_before = self.store.get_collect_cursor(provider_key, scope_key) if provider_key else ""
        self.store.record_collect_failure(
            provider_key=provider_key or "provider_unavailable",
            scope_key=scope_key,
            cursor_before=cursor_before,
            error_code=error_code,
            retryable=False,
            raw_payload={"job": redact_provider_payload(dict(job))},
        )
        return SchedulerJobResult(
            requested_provider_key=provider_key,
            provider_key=provider_key or "provider_unavailable",
            capability=capability,
            scope_key=scope_key,
            status=HEALTH_DEGRADED,
            attempts=0,
            error_code=error_code,
            cursor_before=cursor_before,
            cursor_after=cursor_before,
        )

    def _candidate_provider_keys(self, job: Mapping[str, Any], *, capability: str, stamp: int) -> list[str]:
        requested_provider_key = str(job.get("provider_key") or "").strip()
        candidate_keys: list[str] = []
        if requested_provider_key and not self._disabled_reason(requested_provider_key):
            candidate_keys.append(requested_provider_key)

        if _truthy(job.get("fallback_enabled"), default=True):
            for spec in self.registry.providers_for(capability):
                if spec.provider_key not in candidate_keys:
                    candidate_keys.append(spec.provider_key)

        return [
            provider_key
            for provider_key in candidate_keys
            if not self._provider_in_cooldown_or_disabled(provider_key, stamp=stamp)
        ]

    def _disabled_reason(self, provider_key: str) -> str:
        if not provider_key:
            return ""
        try:
            status = self.registry.status_for(provider_key)
        except KeyError:
            return ""
        if status.enabled:
            return ""
        return status.reason or "provider_disabled"

    def _provider_in_cooldown_or_disabled(self, provider_key: str, *, stamp: int) -> bool:
        health = self._health_by_provider().get(provider_key)
        if not health:
            return False
        status = str(health.get("status") or "").upper()
        if status == HEALTH_DISABLED:
            return True
        cooldown_until_ms = _optional_int(health.get("cooldown_until_ms"))
        return status == HEALTH_DEGRADED and cooldown_until_ms is not None and cooldown_until_ms > stamp

    def _health_by_provider(self) -> dict[str, dict[str, Any]]:
        return {str(row.get("provider_key")): row for row in self.store.fetch_provider_health()}

    def _mark_health(
        self,
        provider_key: str,
        status: str,
        *,
        capability: str,
        scope_key: str,
        reason: str,
        latency_ms: float | None = None,
        cooldown_until_ms: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.store.set_provider_health(
            provider_key=redact_provider_text(provider_key),
            status=status,
            reason=redact_provider_text(reason),
            capability=capability,
            scope_key=scope_key,
            latency_ms=latency_ms,
            cooldown_until_ms=cooldown_until_ms,
            payload=payload,
        )

    def _job_due(self, job: Mapping[str, Any], *, stamp: int, force: bool) -> bool:
        if force:
            return True
        interval_ms = int(job.get("interval_ms") or 0)
        if interval_ms <= 0:
            return True
        return stamp >= self._next_due_ms.get(_job_key(job), 0)

    def _schedule_next_due(self, job: Mapping[str, Any], *, stamp: int) -> None:
        interval_ms = int(job.get("interval_ms") or 0)
        if interval_ms <= 0:
            return
        self._next_due_ms[_job_key(job)] = self._next_due_for(job, stamp=stamp)

    def _next_due_for(self, job: Mapping[str, Any], *, stamp: int) -> int | None:
        interval_ms = int(job.get("interval_ms") or 0)
        if interval_ms <= 0:
            return None
        jitter_ms = max(0, int(job.get("jitter_ms") or 0))
        return stamp + interval_ms + int(self.random_fn() * jitter_ms)


def _env_enabled(environ: Mapping[str, str]) -> bool:
    return str(environ.get(LIVE_COLLECTORS_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


def _private_capability(capability: str) -> bool:
    lowered = capability.lower()
    return any(fragment in lowered for fragment in _PRIVATE_CAPABILITY_FRAGMENTS)


def _backoff_for_attempt(backoff_ms: int, attempt_no: int) -> int:
    return max(0, int(backoff_ms)) * (2 ** max(0, int(attempt_no) - 1))


def _job_key(job: Mapping[str, Any]) -> str:
    return ":".join(
        (
            str(job.get("provider_key") or "*"),
            normalize_capability(str(job.get("capability") or "")),
            str(job.get("scope_key") or "default"),
        )
    )


def _optional_int(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _truthy(value: Any, *, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off", "disabled"}
    return default


def _job_enabled(job: Mapping[str, Any]) -> bool:
    raw = job.get("enabled", True)
    return _truthy(raw, default=True)
