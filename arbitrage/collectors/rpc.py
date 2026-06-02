from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from arbitrage.store import ArbitrageStore

from .base import (
    STATUS_DEGRADED,
    STATUS_OK,
    CollectorResult,
    ProviderPayloadError,
    ensure_payload_mapping,
    monotonic_cursor_value,
    normalize_observed_at_ms,
    provider_payload_error,
)


@dataclass(frozen=True, slots=True)
class RpcFreshnessObservation:
    latest_block: int
    observed_at_ms: int
    payload: dict[str, Any]

    @property
    def cursor_value(self) -> str:
        return str(self.latest_block)


def ingest_rpc_freshness_fixture(
    store: ArbitrageStore,
    payload: Any,
    *,
    provider_key: str = "rpc_public",
    scope_key: str = "rpc_block",
    now_ms: int | None = None,
) -> CollectorResult:
    provider_key = str(provider_key or "rpc_public").strip()
    scope_key = str(scope_key or "rpc_block").strip()
    cursor_before = store.get_collect_cursor(provider_key, scope_key)

    try:
        payload_map = ensure_payload_mapping(payload, provider_key=provider_key, scope_key=scope_key)
        observation = _parse_observation(
            payload_map,
            provider_key=provider_key,
            scope_key=scope_key,
            now_ms=now_ms,
        )
    except ProviderPayloadError as exc:
        store.record_collect_failure(
            provider_key=provider_key,
            scope_key=scope_key,
            cursor_before=cursor_before,
            error_code=exc.error_code,
            retryable=exc.error_code in {"rpc_timeout", "rpc_partial_failure", "rpc_result_null"},
            raw_payload=exc.to_deadletter_payload(),
        )
        return CollectorResult(
            provider_key=provider_key,
            scope_key=scope_key,
            cursor_before=cursor_before,
            cursor_after=cursor_before,
            status=STATUS_DEGRADED,
            inserted_count=0,
            deadletter_count=1,
        )

    cursor_after = monotonic_cursor_value(cursor_before, observation.cursor_value)
    store.record_collect_success(
        provider_key=provider_key,
        scope_key=scope_key,
        cursor_value=cursor_after,
        collected_count=1,
        inserted_count=0,
    )
    return CollectorResult(
        provider_key=provider_key,
        scope_key=scope_key,
        cursor_before=cursor_before,
        cursor_after=cursor_after,
        status=STATUS_OK,
        inserted_count=0,
        deadletter_count=0,
    )


def _parse_observation(
    payload: Mapping[str, Any],
    *,
    provider_key: str,
    scope_key: str,
    now_ms: int | None,
) -> RpcFreshnessObservation:
    failure_code = _payload_failure_code(payload)
    if failure_code:
        raise _payload_error(
            provider_key=provider_key,
            scope_key=scope_key,
            error_code=failure_code,
            message=f"RPC freshness payload failed with {failure_code}",
            field_path="payload",
            payload=payload,
        )

    latest_block = _latest_block(payload)
    if latest_block is None or latest_block <= 0:
        raise _payload_error(
            provider_key=provider_key,
            scope_key=scope_key,
            error_code="malformed_rpc_payload",
            message="RPC freshness payload is missing a positive latest block number",
            field_path="payload.result",
            payload=payload,
        )

    observed_at_ms = _observed_at_ms(
        _first_value(
            payload,
            ("observed_at_ms", "observedAt", "timestamp", "ts", "time"),
            _first_value(_result_mapping(payload), ("observed_at_ms", "observedAt", "timestamp", "ts", "time")),
        ),
        provider_key=provider_key,
        scope_key=scope_key,
        field_path="payload.observed_at_ms",
        payload=payload,
        now_ms=now_ms,
    )
    return RpcFreshnessObservation(
        latest_block=latest_block,
        observed_at_ms=observed_at_ms,
        payload={
            "latest_block": latest_block,
            "observed_at_ms": observed_at_ms,
        },
    )


def _payload_failure_code(payload: Mapping[str, Any]) -> str:
    explicit_error_code = _text(_first_value(payload, ("error_code", "errorCode", "code")))
    if explicit_error_code in {"rpc_result_null", "rpc_timeout", "malformed_rpc_payload", "rpc_partial_failure"}:
        return explicit_error_code
    if explicit_error_code == "timeout":
        return "rpc_timeout"

    if _truthy(_first_value(payload, ("timeout", "timed_out", "timedOut"))):
        return "rpc_timeout"

    status = _text(_first_value(payload, ("status", "state"))).lower()
    if status in {"timeout", "timed_out"}:
        return "rpc_timeout"
    if status in {"partial", "partial_failure", "degraded"}:
        return "rpc_partial_failure"

    if _truthy(_first_value(payload, ("partial_failure", "partialFailure", "partial"))):
        return "rpc_partial_failure"

    if "result" in payload and payload.get("result") is None:
        return "rpc_result_null"

    error = payload.get("error")
    if isinstance(error, Mapping):
        message = _text(_first_value(error, ("message", "error", "reason"))).lower()
        if "timeout" in message:
            return "rpc_timeout"
        return "rpc_partial_failure"
    if error not in (None, "", False):
        return "rpc_partial_failure"

    return ""


def _latest_block(payload: Mapping[str, Any]) -> int | None:
    direct = _block_number(
        _first_value(
            payload,
            ("latest_block", "latestBlock", "block_number", "blockNumber", "number", "height"),
        )
    )
    if direct is not None:
        return direct

    result = payload.get("result")
    if isinstance(result, Mapping):
        return _block_number(
            _first_value(result, ("latest_block", "latestBlock", "block_number", "blockNumber", "number", "height"))
        )
    return _block_number(result)


def _result_mapping(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    result = payload.get("result")
    return result if isinstance(result, Mapping) else {}


def _block_number(value: Any) -> int | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text, 16) if text.lower().startswith("0x") else int(text)
        except ValueError:
            return None
    return None


def _observed_at_ms(
    value: Any,
    *,
    provider_key: str,
    scope_key: str,
    field_path: str,
    payload: Mapping[str, Any],
    now_ms: int | None,
) -> int:
    try:
        return normalize_observed_at_ms(value, now_ms=now_ms)
    except ProviderPayloadError as exc:
        raise _payload_error(
            provider_key=provider_key,
            scope_key=scope_key,
            error_code="malformed_rpc_payload",
            message=exc.message,
            field_path=field_path,
            payload=payload,
        ) from exc


def _first_value(payload: Mapping[str, Any], keys: tuple[str, ...], fallback: Any = None) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            return value
    return fallback


def _text(value: Any) -> str:
    return str(value or "").strip()


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    if isinstance(value, (int, float)):
        return bool(value)
    return False


def _payload_error(
    *,
    provider_key: str,
    scope_key: str,
    error_code: str,
    message: str,
    field_path: str,
    payload: Mapping[str, Any],
) -> ProviderPayloadError:
    return provider_payload_error(
        provider_key=provider_key,
        scope_key=scope_key,
        error_code=error_code,
        message=message,
        field_path=field_path,
        payload=payload,
    )
